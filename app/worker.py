import argparse
import logging
import signal
import socket
import time
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.models import ProcessingRun, RunStatus, WorkerHeartbeat
from app.services.runs import claim_next_run
from app.time import utc_now

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("gamerate.worker")
stopping = False


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


def execute_placeholder(db: Session, run: ProcessingRun) -> None:
    """Complete infrastructure jobs without performing external collection yet."""
    logger.info("claimed run id=%s trigger=%s", run.id, run.trigger.value)
    run.message = "Worker accepted job; collectors are not implemented yet"
    run.progress_total = 0
    run.progress_current = 0
    run.updated_at = utc_now()
    db.commit()

    run.status = RunStatus.SUCCEEDED
    run.finished_at = utc_now()
    run.updated_at = run.finished_at
    run.message = "No-op processing completed"
    db.commit()
    logger.info("completed run id=%s status=%s", run.id, run.status.value)


def work_once(worker_id: str, started_at: datetime) -> bool:
    with SessionLocal() as db:
        heartbeat(db, worker_id, started_at)
        run = claim_next_run(db, worker_id)
        if run is None:
            return False
        heartbeat(db, worker_id, started_at, run.id)
        try:
            execute_placeholder(db, run)
        except Exception as exc:
            db.rollback()
            failed = db.scalar(select(ProcessingRun).where(ProcessingRun.id == run.id))
            if failed:
                failed.status = RunStatus.FAILED
                failed.error = str(exc)
                failed.finished_at = utc_now()
                failed.updated_at = failed.finished_at
                db.commit()
            logger.exception("run failed id=%s", run.id)
        finally:
            heartbeat(db, worker_id, started_at)
        return True


def run_loop(worker_id: str, once: bool = False) -> None:
    started_at = utc_now()
    logger.info("worker started id=%s", worker_id)
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
