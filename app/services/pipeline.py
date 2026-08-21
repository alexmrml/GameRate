"""The single processing path used by manual and scheduled runs alike.

The worker owns the transport; this module owns the sequence: pick today's next games,
collect each one, persist it, and keep the run row current so the Activity page and its
SSE stream show live progress. A failing game is recorded and skipped — it never aborts
the batch — while a failing discovery step fails the run outright rather than reporting
an empty success.
"""

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.collectors.metacritic import MetacriticClient
from app.config import settings
from app.models import CrawlStatus, ProcessingRun, RunStatus
from app.services.crawl import (
    finish_state,
    get_or_create_state,
    mark_game_processed,
    mark_slug_failed,
    plan_next_batch,
    start_state,
)
from app.services.games import apply_game_snapshot
from app.time import utc_now

logger = logging.getLogger("gamerate.pipeline")
MAX_TRACKED_ERRORS = 20


def _touch(db: Session, run: ProcessingRun, message: str | None = None, **fields: Any) -> None:
    """Publish progress immediately; the Activity SSE stream reads committed rows."""
    if message is not None:
        run.message = message[:1000]
    for name, value in fields.items():
        setattr(run, name, value)
    run.updated_at = utc_now()
    db.commit()


def execute_run(db: Session, run: ProcessingRun, client: MetacriticClient) -> ProcessingRun:
    """Run one Metacritic batch and leave ``run`` in a final state."""
    state = get_or_create_state(db)
    start_state(db, state, run.id)
    _touch(db, run, "Selecting games from Metacritic", progress_current=0, progress_total=None)

    try:
        plan = plan_next_batch(db, client, state)
    except Exception as exc:  # discovery is fatal: never report an empty success
        db.rollback()
        logger.exception("discovery failed run=%s", run.id)
        state = get_or_create_state(db)
        finish_state(db, state, {}, CrawlStatus.FAILED)
        _touch(
            db,
            run,
            f"Discovery failed: {exc}",
            status=RunStatus.FAILED,
            error=str(exc),
            finished_at=utc_now(),
        )
        return run

    details: dict[str, Any] = {
        "stage": plan.stage,
        "processing_date": state.processing_date.isoformat(),
        "planned": len(plan.slugs),
        **plan.details,
    }
    _touch(
        db,
        run,
        f"Processing {len(plan.slugs)} games from the {plan.stage.replace('_', ' ')} stage",
        progress_total=len(plan.slugs),
        details=details,
    )

    succeeded = 0
    errors: list[dict[str, str]] = []

    for index, slug in enumerate(plan.slugs, start=1):
        _touch(db, run, f"Collecting {slug} ({index}/{len(plan.slugs)})")
        try:
            snapshot = client.collect_game(slug)
            game = apply_game_snapshot(db, snapshot)
            mark_game_processed(
                db,
                state,
                game_id=game.id,
                run_id=run.id,
                status=RunStatus.SUCCEEDED,
                details={
                    "slug": slug,
                    "stage": plan.stage,
                    "platforms": len(snapshot.platforms),
                    "reviews": len(snapshot.reviews),
                },
            )
            succeeded += 1
            _touch(
                db,
                run,
                f"Saved {snapshot.title} ({index}/{len(plan.slugs)})",
                progress_current=index,
                current_game_id=game.id,
            )
        except Exception as exc:  # one bad game must not lose the rest of the batch
            db.rollback()
            logger.exception("game processing failed slug=%s run=%s", slug, run.id)
            state = get_or_create_state(db)
            mark_slug_failed(db, state, slug)
            errors.append({"slug": slug, "error": f"{type(exc).__name__}: {exc}"[:500]})
            _touch(
                db,
                run,
                f"Failed {slug} ({index}/{len(plan.slugs)}): {exc}",
                progress_current=index,
                current_game_id=None,
            )

    # A fresh dict, because SQLAlchemy tracks JSON columns by identity, not by content.
    details = {
        **details,
        "succeeded": succeeded,
        "failed": len(errors),
        "errors": errors[:MAX_TRACKED_ERRORS],
        "next_cursor": plan.next_cursor,
    }

    if not plan.slugs:
        status = RunStatus.SUCCEEDED
        message = "No unprocessed games left for today"
    elif succeeded == 0:
        status = RunStatus.FAILED
        message = f"All {len(plan.slugs)} games failed"
    elif errors:
        status = RunStatus.SUCCEEDED
        message = f"Processed {succeeded} of {len(plan.slugs)} games, {len(errors)} failed"
    else:
        status = RunStatus.SUCCEEDED
        message = f"Processed {succeeded} games"

    finish_state(
        db,
        state,
        plan.next_cursor,
        CrawlStatus.FAILED if status is RunStatus.FAILED else CrawlStatus.COMPLETED,
    )
    _touch(
        db,
        run,
        message,
        status=status,
        current_game_id=None,
        error="; ".join(item["error"] for item in errors[:5]) or None,
        details=details,
        finished_at=utc_now(),
    )
    logger.info(
        "run finished id=%s stage=%s succeeded=%s failed=%s",
        run.id,
        plan.stage,
        succeeded,
        len(errors),
    )
    return run


def build_client() -> MetacriticClient:
    return MetacriticClient(base_url=settings.metacritic_base_url)
