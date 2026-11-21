"""job_run logging against real Postgres, inside a rolled-back transaction."""

import uuid

import pytest
from sqlalchemy import select

from prawdowid.domain.enums import JobStatus
from prawdowid.persistence.job_log import MAX_PROBLEMS, track_job
from prawdowid.persistence.models import JobRun


def load(sessions, job_id: uuid.UUID) -> JobRun:
    with sessions() as session:
        return session.get_one(JobRun, job_id)


def test_successful_job_records_summary_and_timing(isolated_sessions) -> None:
    with track_job("test.ok", trigger="cli", params={"source": "gov-rcb", "id": uuid.uuid4()},
                   session_factory=isolated_sessions) as job:
        assert load(isolated_sessions, job.id).status is JobStatus.RUNNING  # visible while running
        job.record({"new": 3, "skipped": 1})
    row = load(isolated_sessions, job.id)
    assert row.status is JobStatus.OK
    assert row.result == {"new": 3, "skipped": 1}
    assert row.params["source"] == "gov-rcb" and isinstance(row.params["id"], str)  # JSON-safe
    assert row.finished_at is not None and row.duration_ms is not None and row.duration_ms >= 0
    assert row.error is None and row.problems == [] and row.trigger == "cli" and row.worker


def test_reported_problems_mark_job_problems(isolated_sessions) -> None:
    with track_job("test.problems", trigger="celery", task_id="abc", worker="w@h",
                   session_factory=isolated_sessions) as job:
        job.record({"failed": 2}, problems=[f"doc {i} failed" for i in range(MAX_PROBLEMS + 20)])
    row = load(isolated_sessions, job.id)
    assert row.status is JobStatus.PROBLEMS
    assert len(row.problems) == MAX_PROBLEMS  # capped
    assert (row.task_id, row.worker) == ("abc", "w@h")


def test_explicit_ok_overrides_informational_problems(isolated_sessions) -> None:
    with track_job("test.notes", trigger="cli", session_factory=isolated_sessions) as job:
        job.record({"skipped": 1}, problems=["skipped video without text"], ok=True)
    row = load(isolated_sessions, job.id)
    assert row.status is JobStatus.OK and row.problems == ["skipped video without text"]


def test_crash_is_recorded_with_traceback_and_reraised(isolated_sessions) -> None:
    job_ids = []
    with pytest.raises(RuntimeError, match="source down"):
        with track_job("test.crash", trigger="celery", session_factory=isolated_sessions) as job:
            job_ids.append(job.id)
            raise RuntimeError("source down")
    row = load(isolated_sessions, job_ids[0])
    assert row.status is JobStatus.FAILED
    assert row.error == "RuntimeError: source down"
    assert "Traceback" in row.traceback and "source down" in row.traceback
    assert row.finished_at is not None


def test_unavailable_log_never_breaks_the_job() -> None:
    def broken_factory():
        raise ConnectionError("database down")

    ran = []
    with track_job("test.nodb", trigger="cli", session_factory=broken_factory) as job:
        ran.append(True)
        job.record({"new": 1})
    assert ran == [True] and job.id is None


def test_ingestion_run_links_back_to_its_job(isolated_sessions) -> None:
    from test_ingestion_service import LONG, SLUG, doc, make_service

    from prawdowid.persistence.models import IngestionRun

    service = make_service(isolated_sessions, {"default": [doc("https://e.gov.pl/a", LONG)]})
    with track_job("test.ingest", trigger="cli", session_factory=isolated_sessions) as job:
        result = service.ingest_source(SLUG, job_id=job.id)
        job.record({"new": result.counts["new"]})
    with isolated_sessions() as session:
        run = session.get_one(IngestionRun, result.run_id)
        assert run.job_id == job.id
        linked = session.scalars(select(IngestionRun).where(IngestionRun.job_id == job.id)).all()
        assert len(linked) == 1
