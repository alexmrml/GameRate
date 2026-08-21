import argparse
import logging
import signal
import socket
import time
from collections.abc import Callable
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.collectors.metacritic import MetacriticClient
from app.config import settings
from app.db import SessionLocal
from app.models import ProcessingRun, RunStatus, WorkerHeartbeat
from app.services.crawl import release_state
from app.services.pipeline import build_client, execute_run
from app.services.runs import claim_next_run, ensure_scheduled_run, recover_stale_runs
from app.time import utc_now

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("gamerate.worker")
stopping = False

ClientFactory = Callable[[], MetacriticClient]


def request_stop(_signum: int, _frame: object) -> None:
    global stopping
    stopping = True


def heartbeat(db: Session, worker_id: str, started_at: datetime, run_id: object = None) -> None:
    row = db.get(WorkerHeartbeat, worker_id)
    now = utc_now()
    if row is None:
        row = WorkerHeartbeat(
            worker_id=worker_id,
            started_at=started_at,
            last_seen_at=now,
            details={"host": socket.gethostname()},
        )
        db.add(row)
    row.last_seen_at = now
    row.current_run_id = run_id
    db.commit()


def fail_run(db: Session, run_id: object, error: Exception) -> None:
    db.rollback()
    failed = db.scalar(select(ProcessingRun).where(ProcessingRun.id == run_id))
    if failed is None or failed.status in {RunStatus.SUCCEEDED, RunStatus.FAILED}:
        return
    now = utc_now()
    failed.status = RunStatus.FAILED
    failed.error = str(error)
    failed.message = f"Run failed: {error}"[:1000]
    failed.finished_at = now
    failed.updated_at = now
    release_state(db, failed.id)
    db.commit()


def work_once(
    worker_id: str,
    started_at: datetime,
    *,
    client_factory: ClientFactory = build_client,
    schedule: bool = True,
) -> bool:
    """Poll once: recover abandoned work, keep the schedule, then run one job."""
    with SessionLocal() as db:
        heartbeat(db, worker_id, started_at)
        recover_stale_runs(db)
        if schedule:
            ensure_scheduled_run(db)
        run = claim_next_run(db, worker_id)
        if run is None:
            return False
        heartbeat(db, worker_id, started_at, run.id)
        client: MetacriticClient | None = None
        try:
            client = client_factory()
            execute_run(db, run, client)
        except Exception as exc:
            logger.exception("run failed id=%s", run.id)
            fail_run(db, run.id, exc)
        finally:
            if client is not None:
                client.close()
            heartbeat(db, worker_id, started_at)
        return True


def run_loop(worker_id: str, once: bool = False) -> None:
    started_at = utc_now()
    logger.info("worker started id=%s", worker_id)
    with SessionLocal() as db:
        for run_id in recover_stale_runs(db, worker_id):
            logger.warning("recovered interrupted run id=%s", run_id)
    while not stopping:
        handled = work_once(worker_id, started_at)
        if once:
            break
        if not handled:
            time.sleep(settings.worker_poll_seconds)
    logger.info("worker stopped id=%s", worker_id)


def main() -> None:
    parser = argparse.ArgumentParser(prog="gamerate-worker")
    parser.add_argument("--once", action="store_true", help="Poll once and exit")
    parser.add_argument("--worker-id", default=settings.worker_id)
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    run_loop(args.worker_id, once=args.once)


if __name__ == "__main__":
    main()
