import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db import SessionLocal
from app.models import ProcessingRun, RunStatus, WorkerHeartbeat
from app.services.runs import enqueue_manual_run
from app.time import utc_now
from app.worker import work_once


def test_worker_claims_manual_run_and_writes_heartbeat(user: object) -> None:
    with SessionLocal() as db:
        run = enqueue_manual_run(db, user.id)
        run_id = run.id

    assert work_once("test-worker", utc_now()) is True

    with SessionLocal() as db:
        completed = db.get(ProcessingRun, run_id)
        heartbeat = db.get(WorkerHeartbeat, "test-worker")
        assert completed is not None
        assert completed.status == RunStatus.SUCCEEDED
        assert completed.worker_id == "test-worker"
        assert heartbeat is not None
        assert heartbeat.current_run_id is None


def test_worker_returns_false_without_work() -> None:
    assert work_once("idle-worker", utc_now()) is False
    with SessionLocal() as db:
        assert db.scalar(select(WorkerHeartbeat).where(WorkerHeartbeat.worker_id == "idle-worker"))


def test_database_rejects_two_running_jobs(user: object) -> None:
    with SessionLocal() as db:
        first = enqueue_manual_run(db, user.id)
        second = enqueue_manual_run(db, user.id)
        first.status = RunStatus.RUNNING
        db.commit()
        second.status = RunStatus.RUNNING
        with pytest.raises(IntegrityError):
            db.commit()
