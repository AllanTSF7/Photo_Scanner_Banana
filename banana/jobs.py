"""Minimal SQLite-backed job queue shared by the API and the GPU worker process."""

from __future__ import annotations

import logging
import threading
from typing import Callable

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlmodel import Session

from banana.models import Job, utcnow

log = logging.getLogger(__name__)

Handler = Callable[[Session, Job], None]
HANDLERS: dict[str, Handler] = {}
MAX_ATTEMPTS = 3


def handler(job_type: str) -> Callable[[Handler], Handler]:
    def register(fn: Handler) -> Handler:
        HANDLERS[job_type] = fn
        return fn

    return register


def enqueue(session: Session, job_type: str, scan_id: int | None = None) -> Job:
    job = Job(type=job_type, scan_id=scan_id)
    session.add(job)
    session.commit()
    return job


def claim_next(engine: Engine) -> int | None:
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "UPDATE job SET state='running', attempts=attempts+1, updated_at=:now "
                "WHERE id=(SELECT id FROM job WHERE state='queued' ORDER BY id LIMIT 1) RETURNING id"
            ),
            {"now": utcnow().strftime("%Y-%m-%d %H:%M:%S.%f")},  # SQLAlchemy's SQLite DateTime format
        ).first()
    return row[0] if row else None


def run_one(engine: Engine, job_id: int) -> None:
    with Session(engine) as session:
        job = session.get(Job, job_id)
        try:
            fn = HANDLERS.get(job.type)
            if fn is None:
                raise LookupError(f"no handler for job type {job.type!r}")
            fn(session, job)
            job.state, job.error = "done", None
        except Exception as exc:  # noqa: BLE001 - recorded on the job row
            log.exception("job %s failed", job_id)
            session.rollback()
            job = session.get(Job, job_id)
            job.state = "queued" if job.attempts < MAX_ATTEMPTS else "failed"
            job.error = repr(exc)
        job.updated_at = utcnow()
        session.add(job)
        session.commit()


def run_worker(engine: Engine, stop: threading.Event, poll_seconds: float = 2.0) -> None:
    while not stop.is_set():
        job_id = claim_next(engine)
        if job_id is None:
            stop.wait(poll_seconds)
            continue
        run_one(engine, job_id)
