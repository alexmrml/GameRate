"""The single processing path used by manual and scheduled runs alike.

The worker owns the transport; this module owns the sequence: pick today's next games,
collect each one, persist it, and keep the run row current so the Activity page and its
SSE stream show live progress. A failing game is recorded and skipped — it never aborts
the batch — while a failing discovery step fails the run outright rather than reporting
an empty success.
"""

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.collectors.metacritic import MetacriticClient
from app.config import settings
from app.models import CrawlStatus, Game, GamePlatform, ProcessingRun, RunStatus
from app.services.crawl import (
    finish_state,
    get_or_create_state,
    mark_game_processed,
    mark_slug_failed,
    plan_next_batch,
    start_state,
)
from app.services.enrichment import EnrichmentSession, GameEnrichment
from app.services.games import apply_game_snapshot
from app.services.youtube import (
    NO_CANDIDATE,
    NO_USEFUL_COMMENTARY,
    SEARCH_BUDGET,
    SUCCESS,
    UNCHANGED,
    YouTubeEnrichmentSession,
    YouTubeOutcome,
    youtube_needs_work,
)
from app.time import utc_now

logger = logging.getLogger("gamerate.pipeline")
MAX_TRACKED_ERRORS = 20
MAX_TRACKED_AI_GAMES = 30
MAX_TRACKED_YOUTUBE_GAMES = 30


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
    collected_ids: list[uuid.UUID] = []

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
            collected_ids.append(game.id)
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

    ai_details = enrich_collected_games(db, run, collected_ids, offset=len(plan.slugs))
    youtube_offset = len(plan.slugs) + int(ai_details.get("planned") or 0)
    youtube_details = enrich_youtube_games(db, run, collected_ids, offset=youtube_offset)

    # A fresh dict, because SQLAlchemy tracks JSON columns by identity, not by content.
    details = {
        **details,
        "succeeded": succeeded,
        "failed": len(errors),
        "errors": errors[:MAX_TRACKED_ERRORS],
        "next_cursor": plan.next_cursor,
        "ai": ai_details,
        "youtube": youtube_details,
    }

    ai_note = _ai_note(ai_details)
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
    message = f"{message}{ai_note}{_youtube_note(youtube_details)}"

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


def enrich_collected_games(
    db: Session,
    run: ProcessingRun,
    game_ids: list[uuid.UUID],
    *,
    offset: int,
    session: EnrichmentSession | None = None,
) -> dict[str, Any]:
    """Second phase of a run: ask Gemini about the games this run just collected.

    Enrichment is best-effort by design. A game that fails is recorded and the next game is
    still attempted; only a credential-level failure stops the phase, because repeating that
    request cannot succeed. Nothing here can fail the crawl that already completed.
    """
    session = session or EnrichmentSession(db)
    summary: dict[str, Any] = {
        "enabled": session.enabled,
        "model": session.model,
        "planned": 0,
        "generated": 0,
        "failed": 0,
        "skipped": 0,
        "calls": 0,
        "games": [],
    }
    if not session.enabled:
        summary["disabled_reason"] = session.disabled_reason
        return summary
    if not game_ids:
        return summary

    targets = game_ids[: max(session.max_games, 0)]
    summary["planned"] = len(targets)
    if not targets:
        return summary

    _touch(
        db,
        run,
        f"Analyzing reviews for {len(targets)} games",
        progress_total=offset + len(targets),
    )

    for index, game_id in enumerate(targets, start=1):
        game = db.scalar(
            select(Game)
            .where(Game.id == game_id)
            .options(
                selectinload(Game.genres),
                selectinload(Game.tags),
                selectinload(Game.platforms).selectinload(GamePlatform.platform),
            )
        )
        if game is None:
            continue
        _touch(
            db,
            run,
            f"Analyzing {game.title} ({index}/{len(targets)})",
            current_game_id=game.id,
        )
        try:
            outcome = session.enrich_game(db, game)
            db.commit()
        except Exception as exc:  # enrichment must never fail a completed crawl
            db.rollback()
            logger.exception("enrichment crashed for game=%s run=%s", game_id, run.id)
            outcome = GameEnrichment(title=game.title, error=f"{type(exc).__name__}: {exc}"[:500])

        if outcome.error:
            summary["failed"] += 1
        elif outcome.called_model:
            summary["generated"] += 1
        else:
            summary["skipped"] += 1
        if len(summary["games"]) < MAX_TRACKED_AI_GAMES:
            summary["games"].append(outcome.as_details())

        _touch(db, run, run.message, progress_current=offset + index, current_game_id=None)

        if session.disabled_reason:
            summary["disabled_reason"] = session.disabled_reason
            summary["skipped"] += len(targets) - index
            logger.warning("stopping enrichment for run=%s: %s", run.id, session.disabled_reason)
            break

    summary["calls"] = session.calls
    return summary


def _ai_note(ai_details: dict[str, Any]) -> str:
    if not ai_details.get("enabled"):
        return ""
    parts = []
    if ai_details.get("generated"):
        parts.append(f"{ai_details['generated']} enriched")
    if ai_details.get("failed"):
        failed = ai_details["failed"]
        parts.append(f"{failed} AI failure{'' if failed == 1 else 's'}")
    if ai_details.get("disabled_reason"):
        parts.append("AI stopped")
    return f" · AI: {', '.join(parts)}" if parts else ""


def enrich_youtube_games(
    db: Session,
    run: ProcessingRun,
    collected_ids: list[uuid.UUID],
    *,
    offset: int,
    session: YouTubeEnrichmentSession | None = None,
) -> dict[str, Any]:
    """Analyse current games first, then fill the catalogue backlog without blocking crawl."""
    session = session or YouTubeEnrichmentSession(db)
    summary: dict[str, Any] = {
        "enabled": session.enabled,
        "model": session.model,
        "video_fallback_model": session.video_fallback_model,
        "planned": 0,
        "succeeded": 0,
        "failed": 0,
        "no_candidate": 0,
        "skipped": 0,
        "search_calls": 0,
        "transcript_reads": 0,
        "gemini_calls": 0,
        "video_fallback_calls": 0,
        "games": [],
    }
    if not session.enabled:
        summary["disabled_reason"] = session.disabled_reason
        return summary

    games = list(
        db.scalars(
            select(Game)
            .options(selectinload(Game.youtube_analysis))
            .order_by(Game.last_discovered_at.desc())
        ).unique()
    )
    by_id = {game.id: game for game in games}
    prioritized = [by_id[game_id] for game_id in collected_ids if game_id in by_id]
    prioritized.extend(game for game in games if game.id not in set(collected_ids))
    targets = [game for game in prioritized if youtube_needs_work(game.youtube_analysis)][
        : max(session.max_games, 0)
    ]
    summary["planned"] = len(targets)
    if not targets:
        session.close()
        return summary

    _touch(
        db,
        run,
        f"Analyzing YouTube let's-plays for {len(targets)} games",
        progress_total=offset + len(targets),
    )

    try:
        for index, game in enumerate(targets, start=1):
            _touch(
                db,
                run,
                f"YouTube analysis for {game.title} ({index}/{len(targets)})",
                current_game_id=game.id,
            )
            try:
                outcome = session.enrich_game(db, game)
                db.commit()
            except Exception as exc:  # the provider phase cannot fail completed collection
                db.rollback()
                logger.exception("YouTube enrichment crashed game=%s run=%s", game.id, run.id)
                outcome = YouTubeOutcome(
                    title=game.title,
                    status="internal_error",
                    error=f"{type(exc).__name__}: {exc}"[:500],
                )

            if outcome.status == SUCCESS:
                summary["succeeded"] += 1
            elif outcome.status == NO_CANDIDATE:
                summary["no_candidate"] += 1
            elif outcome.status in {UNCHANGED, SEARCH_BUDGET}:
                summary["skipped"] += 1
            elif outcome.status == NO_USEFUL_COMMENTARY or outcome.error:
                summary["failed"] += 1
            else:
                summary["skipped"] += 1
            if len(summary["games"]) < MAX_TRACKED_YOUTUBE_GAMES:
                summary["games"].append(outcome.as_details())

            _touch(db, run, run.message, progress_current=offset + index, current_game_id=None)
    finally:
        session.close()

    summary["search_calls"] = session.search_calls
    summary["transcript_reads"] = session.transcript_reads
    summary["gemini_calls"] = session.gemini_calls
    summary["video_fallback_calls"] = session.video_fallback_calls
    if session.youtube_disabled_reason:
        summary["youtube_disabled_reason"] = session.youtube_disabled_reason
    if session.gemini_disabled_reason:
        summary["gemini_disabled_reason"] = session.gemini_disabled_reason
    return summary


def _youtube_note(details: dict[str, Any]) -> str:
    if not details.get("enabled"):
        return ""
    parts = []
    if details.get("succeeded"):
        parts.append(f"{details['succeeded']} analyzed")
    if details.get("no_candidate"):
        parts.append(f"{details['no_candidate']} without a candidate")
    if details.get("failed"):
        parts.append(f"{details['failed']} failed")
    if details.get("video_fallback_calls"):
        parts.append(f"{details['video_fallback_calls']} via video fallback")
    return f" · YouTube: {', '.join(parts)}" if parts else ""


def build_client() -> MetacriticClient:
    return MetacriticClient(base_url=settings.metacritic_base_url)
