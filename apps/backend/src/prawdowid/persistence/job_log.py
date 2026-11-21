"""Persistent log of worker tasks and CLI commands (table ``job_run``).

    with track_job("ingestion.ingest_source", trigger="celery", params={...}) as job:
        result = do_work(job_id=job.id)
        job.record({"new": 3, "skipped": 1}, problems=[...], ok=True)

* the row is inserted (``running``) before the work starts, so even a job that
  crashes immediately leaves a trace;
* an exception marks the job ``failed`` with the error and traceback, then is
  re-raised unchanged;
* a job that finishes but reports problems is ``problems``, otherwise ``ok``;
* writing the log never breaks the job: if the database is unreachable the
  failure is logged and the job runs anyway (``job.id`` is then ``None``).
"""

import json
import logging
import socket
import time
import traceback
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from prawdowid.domain.enums import JobStatus
from prawdowid.persistence.models import JobRun

log = logging.getLogger(__name__)

MAX_PROBLEMS = 100
MAX_PROBLEM_CHARS = 500
MAX_ERROR_CHARS = 2_000
MAX_TRACEBACK_CHARS = 20_000


def _jsonable(value: Any) -> Any:
    """UUIDs, datetimes, enums... -> JSON-safe primitives."""
    return json.loads(json.dumps(value, default=str))


@dataclass
class JobHandle:
    id: uuid.UUID | None
    name: str
    result: dict[str, Any] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    ok: bool = True

    def record(
        self, result: Mapping[str, Any], *, problems: Sequence[str] = (), ok: bool | None = None
    ) -> None:
        """Set the job's summary. ``ok`` defaults to "no problems reported"."""
        self.result = dict(result)
        self.problems = [str(p)[:MAX_PROBLEM_CHARS] for p in problems][:MAX_PROBLEMS]
        self.ok = (not self.problems) if ok is None else ok


@contextmanager
def track_job(
    name: str,
    *,
    trigger: str,
    params: Mapping[str, Any] | None = None,
    task_id: str | None = None,
    worker: str | None = None,
    session_factory: Callable[[], Session] | None = None,
) -> Iterator[JobHandle]:
    if session_factory is None:
        from prawdowid.persistence.db import session_factory as default_factory

        session_factory = default_factory()
    started = time.monotonic()
    handle = JobHandle(id=None, name=name)
    try:
        with session_factory() as session, session.begin():
            row = JobRun(
                name=name,
                trigger=trigger,
                task_id=task_id,
                worker=worker or socket.gethostname(),
                status=JobStatus.RUNNING,
                params=_jsonable(dict(params or {})),
            )
            session.add(row)
            session.flush()
            handle.id = row.id
    except Exception:
        log.warning("Could not record start of job %s", name, exc_info=True)

    log.info("Job %s started (id=%s)", name, handle.id)
    try:
        yield handle
    except BaseException as exc:
        _finish(
            session_factory,
            handle,
            started,
            JobStatus.FAILED,
            error=f"{type(exc).__name__}: {exc}"[:MAX_ERROR_CHARS],
            tb="".join(traceback.format_exception(exc))[-MAX_TRACEBACK_CHARS:],
        )
        log.error("Job %s failed: %s", name, exc)
        raise
    status = JobStatus.OK if handle.ok else JobStatus.PROBLEMS
    _finish(session_factory, handle, started, status)
    log.info("Job %s finished: %s %s", name, status.value, json.dumps(handle.result, default=str)[:500])


def _finish(
    session_factory: Callable[[], Session],
    handle: JobHandle,
    started: float,
    status: JobStatus,
    *,
    error: str | None = None,
    tb: str | None = None,
) -> None:
    if handle.id is None:
        return
    try:
        with session_factory() as session, session.begin():
            row = session.get_one(JobRun, handle.id)
            row.status = status
            row.finished_at = datetime.now(timezone.utc)
            row.duration_ms = int((time.monotonic() - started) * 1000)
            row.result = _jsonable(handle.result)
            row.problems = handle.problems
            row.error = error
            row.traceback = tb
    except Exception:
        log.warning("Could not record end of job %s (%s)", handle.name, handle.id, exc_info=True)
