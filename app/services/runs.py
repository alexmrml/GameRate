import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import ProcessingRun, RunStatus, RunTrigger
from app.time import utc_now


def enqueue_manual_run(db: Session, user_id: uuid.UUID) -> ProcessingRun:
    now = utc_now()
    run = ProcessingRun(
        trigger=RunTrigger.MANUAL,
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
