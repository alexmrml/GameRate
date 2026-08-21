"""Daily Metacritic crawl state.

The traversal position lives in PostgreSQL (``daily_crawl_states.cursor``) rather than
in worker memory, so a container restart resumes the same calendar day where it stopped.

Each calendar day starts over: the first run of the day takes the New Releases carousel,
and every later run of that day continues through the browse listing. A game is processed
at most once per day, tracked by ``daily_processed_games`` for known games and by the
cursor's failed-slug list for games that never made it into the catalogue.
"""

import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.collectors.metacritic import MetacriticClient
from app.config import settings
from app.models import CrawlStatus, DailyCrawlState, DailyProcessedGame, Game, RunStatus
from app.services.games import source_key_for
from app.time import app_today, utc_now

STAGE_NEW_RELEASES = "new_releases"
STAGE_BROWSE = "browse"
MAX_TRACKED_FAILURES = 200


@dataclass(slots=True)
class CrawlPlan:
    """Slugs one run should process, plus the cursor to store once it finishes."""

    stage: str
    slugs: list[str]
    next_cursor: dict[str, Any]
    pages_scanned: int = 0
    details: dict[str, Any] = field(default_factory=dict)


def initial_cursor() -> dict[str, Any]:
    return {
        "stage": STAGE_NEW_RELEASES,
        "browse_page": 1,
        "browse_offset": 0,
        "failed_slugs": [],
    }


def get_or_create_state(db: Session, processing_date: date | None = None) -> DailyCrawlState:
    """Return today's crawl state, starting a fresh cycle on a new calendar day."""
    day = processing_date or app_today()
    state = db.scalar(select(DailyCrawlState).where(DailyCrawlState.processing_date == day))
    if state is None:
        now = utc_now()
        state = DailyCrawlState(
            processing_date=day,
            status=CrawlStatus.PENDING,
            cursor=initial_cursor(),
            updated_at=now,
        )
        db.add(state)
        db.flush()
    return state


def slug_source_key(slug: str) -> str:
    """The catalogue identity a Metacritic slug maps to."""
    return source_key_for(slug, None, slug)


def processed_slugs(db: Session, state: DailyCrawlState) -> set[str]:
    """Slugs already handled today, including ones that failed."""
    rows = db.execute(
        select(Game.source_key)
        .join(DailyProcessedGame, DailyProcessedGame.game_id == Game.id)
        .where(DailyProcessedGame.processing_date == state.processing_date)
    ).scalars()
    handled = {key.split(":", 1)[1] for key in rows if key.startswith("metacritic:")}
    cursor = state.cursor or {}
    handled.update(cursor.get("failed_slugs") or [])
    return handled


def plan_next_batch(
    db: Session,
    client: MetacriticClient,
    state: DailyCrawlState,
    limit: int | None = None,
) -> CrawlPlan:
    """Choose the next games for this run without processing them."""
    batch_size = limit if limit is not None else settings.crawl_batch_size
    cursor = dict(state.cursor or initial_cursor())
    handled = processed_slugs(db, state)

    if cursor.get("stage", STAGE_NEW_RELEASES) == STAGE_NEW_RELEASES:
        slugs = [slug for slug in client.new_release_slugs()[:batch_size] if slug not in handled]
        next_cursor = {**cursor, "stage": STAGE_BROWSE, "browse_page": 1, "browse_offset": 0}
        return CrawlPlan(
            stage=STAGE_NEW_RELEASES,
            slugs=slugs,
            next_cursor=next_cursor,
            details={"source": "new-releases carousel"},
        )

    page = int(cursor.get("browse_page") or 1)
    offset = int(cursor.get("browse_offset") or 0)
    targets: list[str] = []
    pages_scanned = 0

    while len(targets) < batch_size and pages_scanned < settings.crawl_max_browse_pages_per_run:
        listing = client.browse_slugs(page)
        pages_scanned += 1
        if not listing:
            break
        exhausted = True
        for index, slug in enumerate(listing[offset:], start=offset + 1):
            if slug not in handled and slug not in targets:
                targets.append(slug)
            if len(targets) >= batch_size:
                offset = index
                exhausted = False
                break
        if exhausted:
            page += 1
            offset = 0

    next_cursor = {**cursor, "stage": STAGE_BROWSE, "browse_page": page, "browse_offset": offset}
    return CrawlPlan(
        stage=STAGE_BROWSE,
        slugs=targets,
        next_cursor=next_cursor,
        pages_scanned=pages_scanned,
        details={"source": "browse listing", "pages_scanned": pages_scanned},
    )


def mark_game_processed(
    db: Session,
    state: DailyCrawlState,
    *,
    game_id: uuid.UUID,
    run_id: uuid.UUID | None,
    status: RunStatus,
    details: dict[str, Any] | None = None,
) -> None:
    """Record a game as handled today; the unique index keeps it to one row per day."""
    row = db.scalar(
        select(DailyProcessedGame).where(
            DailyProcessedGame.processing_date == state.processing_date,
            DailyProcessedGame.game_id == game_id,
        )
    )
    now = utc_now()
    if row is None:
        row = DailyProcessedGame(
            processing_date=state.processing_date,
            game_id=game_id,
            run_id=run_id,
            status=status,
            processed_at=now,
            details=details,
        )
        db.add(row)
    else:
        row.run_id = run_id
        row.status = status
        row.processed_at = now
        row.details = details
    db.flush()


def mark_slug_failed(db: Session, state: DailyCrawlState, slug: str) -> None:
    """Keep a failed slug out of the rest of the day without inventing a game row."""
    cursor = dict(state.cursor or initial_cursor())
    failed = list(cursor.get("failed_slugs") or [])
    if slug not in failed:
        failed.append(slug)
    cursor["failed_slugs"] = failed[-MAX_TRACKED_FAILURES:]
    state.cursor = cursor
    state.updated_at = utc_now()
    db.flush()


def start_state(db: Session, state: DailyCrawlState, run_id: uuid.UUID | None) -> None:
    now = utc_now()
    state.status = CrawlStatus.RUNNING
    state.run_id = run_id
    state.started_at = state.started_at or now
    state.updated_at = now
    db.flush()


def finish_state(
    db: Session,
    state: DailyCrawlState,
    cursor: dict[str, Any],
    status: CrawlStatus,
) -> None:
    now = utc_now()
    merged = dict(state.cursor or {})
    # mark_slug_failed may have appended entries after the plan was built, so the
    # live failure list wins over the snapshot the plan carried.
    merged.update({key: value for key, value in cursor.items() if key != "failed_slugs"})
    state.cursor = merged
    state.status = status
    state.completed_at = now
    state.updated_at = now
    db.flush()


def release_state(db: Session, run_id: uuid.UUID) -> None:
    """Free crawl states left RUNNING by an interrupted worker."""
    states = db.scalars(
        select(DailyCrawlState).where(
            DailyCrawlState.status == CrawlStatus.RUNNING, DailyCrawlState.run_id == run_id
        )
    ).all()
    for state in states:
        state.status = CrawlStatus.FAILED
        state.updated_at = utc_now()
    db.flush()
