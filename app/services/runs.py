import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.models import ProcessingRun, RunStatus, RunTrigger
from app.services.crawl import release_state
from app.time import as_utc, utc_now

ACTIVE_STATUSES = (RunStatus.QUEUED, RunStatus.RUNNING)


def _create_run(db: Session, trigger: RunTrigger, user_id: uuid.UUID | None) -> ProcessingRun:
    now = utc_now()
    run = ProcessingRun(
        trigger=trigger,
        status=RunStatus.QUEUED,
        requested_by_id=user_id,
        progress_current=0,
        message="Waiting for worker",
        queued_at=now,
        created_at=now,
        updated_at=now,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def enqueue_manual_run(db: Session, user_id: uuid.UUID) -> ProcessingRun:
    return _create_run(db, RunTrigger.MANUAL, user_id)


def enqueue_scheduled_run(db: Session) -> ProcessingRun:
    return _create_run(db, RunTrigger.SCHEDULED, None)


def ensure_scheduled_run(db: Session) -> ProcessingRun | None:
    """Queue the hourly run when its interval has elapsed and nothing is pending.

    The schedule is derived from the runs table itself, so it survives worker restarts
    and stays correct with the single-running-job database invariant.
    """
    pending = db.scalar(
        select(ProcessingRun).where(ProcessingRun.status.in_(ACTIVE_STATUSES)).limit(1)
    )
    if pending is not None:
        return None

    last = db.scalar(
        select(ProcessingRun)
        .where(ProcessingRun.trigger == RunTrigger.SCHEDULED)
        .order_by(ProcessingRun.queued_at.desc())
        .limit(1)
    )
    if last is not None:
        due_at = as_utc(last.queued_at) + timedelta(minutes=settings.schedule_interval_minutes)
        if utc_now() < due_at:
            return None
    try:
        return enqueue_scheduled_run(db)
    except IntegrityError:
        db.rollback()
        return None


def recover_stale_runs(db: Session, worker_id: str | None = None) -> list[uuid.UUID]:
    """Fail runs abandoned by a crashed or restarted worker so the queue can move on.

    A run is abandoned when its own worker is starting up again (container restart) or
    when it has not reported progress within ``RUN_STALE_SECONDS``.
    """
    cutoff = utc_now() - timedelta(seconds=settings.run_stale_seconds)
    running = db.scalars(
        select(ProcessingRun).where(ProcessingRun.status == RunStatus.RUNNING)
    ).all()

    recovered: list[uuid.UUID] = []
    for run in running:
        owned_by_restarting_worker = worker_id is not None and run.worker_id == worker_id
        if not owned_by_restarting_worker and as_utc(run.updated_at) > cutoff:
            continue
        now = utc_now()
        run.status = RunStatus.FAILED
        run.error = "Run interrupted before completion (worker restart or stall)"
        run.message = "Interrupted; requeue to continue today's crawl"
        run.finished_at = now
        run.updated_at = now
        release_state(db, run.id)
        recovered.append(run.id)
    if recovered:
        db.commit()
    return recovered


def claim_next_run(db: Session, worker_id: str) -> ProcessingRun | None:
    """Atomically claim one job; the DB index permits only one RUNNING row."""
    candidate = db.scalar(
        select(ProcessingRun)
        .where(ProcessingRun.status == RunStatus.QUEUED)
        .order_by(ProcessingRun.queued_at)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if candidate is None:
        db.rollback()
        return None

    now = utc_now()
    candidate.status = RunStatus.RUNNING
    candidate.worker_id = worker_id
    candidate.started_at = now
    candidate.updated_at = now
    candidate.message = "Claimed by worker"
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return None
    db.refresh(candidate)
    return candidate
