from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db import SessionLocal
from app.models import (
    CrawlStatus,
    DailyCrawlState,
    ProcessingRun,
    RunStatus,
    RunTrigger,
    WorkerHeartbeat,
)
from app.services.runs import (
    enqueue_manual_run,
    enqueue_scheduled_run,
    ensure_scheduled_run,
    recover_stale_runs,
)
from app.time import as_utc, utc_now
from app.worker import _heartbeat_loop, heartbeat, work_once
from tests.conftest import StubMetacriticClient


@pytest.fixture
def collector() -> StubMetacriticClient:
    return StubMetacriticClient(new_releases=["alpha", "beta"])


def test_worker_runs_a_manual_job_through_the_pipeline(
    user: object, collector: StubMetacriticClient
) -> None:
    with SessionLocal() as db:
        run_id = enqueue_manual_run(db, user.id).id

    assert work_once("test-worker", utc_now(), client_factory=lambda: collector) is True

    with SessionLocal() as db:
        completed = db.get(ProcessingRun, run_id)
        heartbeat = db.get(WorkerHeartbeat, "test-worker")
        assert completed.status == RunStatus.SUCCEEDED
        assert completed.worker_id == "test-worker"
        assert completed.message == "Processed 2 games"
        assert heartbeat is not None
        assert heartbeat.current_run_id is None
    assert collector.collected == ["alpha", "beta"]
    assert collector.closed is True


def test_worker_returns_false_without_work() -> None:
    assert work_once("idle-worker", utc_now(), schedule=False) is False
    with SessionLocal() as db:
        assert db.scalar(select(WorkerHeartbeat).where(WorkerHeartbeat.worker_id == "idle-worker"))


def test_busy_worker_refreshes_heartbeat_between_pipeline_updates() -> None:
    started_at = utc_now() - timedelta(hours=1)
    with SessionLocal() as db:
        run = enqueue_scheduled_run(db)
        run.status = RunStatus.RUNNING
        run.worker_id = "busy-worker"
        db.commit()
        heartbeat(db, "busy-worker", started_at, run.id)
        row = db.get(WorkerHeartbeat, "busy-worker")
        row.last_seen_at = started_at
        db.commit()
        run_id = run.id

    class StopAfterOnePulse:
        calls = 0

        def wait(self, _interval: float) -> bool:
            self.calls += 1
            return self.calls > 1

    _heartbeat_loop(
        StopAfterOnePulse(),  # type: ignore[arg-type]
        "busy-worker",
        started_at,
        run_id,
        interval=0.001,
    )

    with SessionLocal() as db:
        refreshed = db.get(WorkerHeartbeat, "busy-worker")
        assert as_utc(refreshed.last_seen_at) > started_at
        assert refreshed.current_run_id == run_id


def test_database_rejects_two_running_jobs(user: object) -> None:
    with SessionLocal() as db:
        first = enqueue_manual_run(db, user.id)
        second = enqueue_manual_run(db, user.id)
        first.status = RunStatus.RUNNING
        db.commit()
        second.status = RunStatus.RUNNING
        with pytest.raises(IntegrityError):
            db.commit()


def test_hourly_schedule_queues_one_run_per_interval() -> None:
    with SessionLocal() as db:
        first = ensure_scheduled_run(db)
        assert first is not None
        assert first.trigger is RunTrigger.SCHEDULED

        first.status = RunStatus.SUCCEEDED
        db.commit()
        assert ensure_scheduled_run(db) is None

        first.queued_at = utc_now() - timedelta(hours=2)
        db.commit()
        assert ensure_scheduled_run(db) is not None


def test_schedule_waits_while_a_run_is_still_pending() -> None:
    with SessionLocal() as db:
        enqueue_scheduled_run(db)
        assert ensure_scheduled_run(db) is None


def test_restart_recovers_a_run_left_running_by_this_worker(user: object) -> None:
    with SessionLocal() as db:
        run = enqueue_manual_run(db, user.id)
        run.status = RunStatus.RUNNING
        run.worker_id = "worker-1"
        db.add(
            DailyCrawlState(
                processing_date=utc_now().date(),
                status=CrawlStatus.RUNNING,
                run_id=run.id,
                cursor={"stage": "browse"},
                updated_at=utc_now(),
            )
        )
        db.commit()
        run_id = run.id

        assert recover_stale_runs(db, "worker-1") == [run_id]
        recovered = db.get(ProcessingRun, run_id)
        assert recovered.status is RunStatus.FAILED
        assert recovered.error
        state = db.scalar(select(DailyCrawlState))
        assert state.status is CrawlStatus.FAILED
        # The queue is free again, so the next scheduled run can start.
        assert ensure_scheduled_run(db) is not None


def test_a_stalled_run_from_another_worker_is_recovered_after_the_timeout(user: object) -> None:
    with SessionLocal() as db:
        run = enqueue_manual_run(db, user.id)
        run.status = RunStatus.RUNNING
        run.worker_id = "worker-2"
        run.updated_at = utc_now() - timedelta(seconds=30)
        db.commit()

        assert recover_stale_runs(db, "worker-1") == []

        run.updated_at = utc_now() - timedelta(hours=1)
        db.commit()
        assert recover_stale_runs(db, "worker-1") == [run.id]
