"""Celery tasks for corpus ingestion and embedding.

The tasks are independent: ingestion only writes documents/passages, embedding
only fills rows whose vector is NULL. Neither enqueues the other, so they can be
scheduled, retried and scaled separately.

Every task execution is recorded in ``job_run`` (see ``persistence.job_log``):
parameters, timing, a summary of what was downloaded/stored/embedded, reported
problems and, on a crash, the error with its traceback.
"""

import logging
from dataclasses import asdict
from typing import Any

from celery import Task

from prawdowid.domain.enums import IngestionStatus
from prawdowid.ingestion.catalog import load_catalog, sync_catalog
from prawdowid.ingestion.service import IngestionResult, IngestionService
from prawdowid.persistence.db import session_factory
from prawdowid.persistence.job_log import track_job
from prawdowid.services.embedding import EmbedderUnavailable, default_embedder, embed_pending
from prawdowid.workers.celery_app import app

log = logging.getLogger(__name__)


def ingestion_summary(result: IngestionResult) -> dict[str, Any]:
    return {"source": result.slug, "status": result.status.value, "ingestion_run_id": str(result.run_id),
            **result.counts}


def _job(task: Task, name: str, params: dict[str, Any]):
    return track_job(name, trigger="celery", params=params, task_id=task.request.id,
                     worker=task.request.hostname)


@app.task(name="ingestion.ingest_source", queue="ingestion", acks_late=True, bind=True)
def ingest_source(self: Task, slug: str, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    with _job(self, "ingestion.ingest_source", {"source": slug, "overrides": overrides}) as job:
        service = IngestionService(session_factory=session_factory(), catalog=load_catalog())
        result = service.ingest_source(slug, overrides=overrides, job_id=job.id)
        summary = ingestion_summary(result)
        job.record(summary, problems=result.errors, ok=result.status is IngestionStatus.OK)
        log.info("Ingested %s: %s", slug, summary)
        return summary


@app.task(name="ingestion.ingest_all", queue="ingestion", bind=True)
def ingest_all(self: Task, overrides: dict[str, Any] | None = None) -> list[str]:
    """Sync the catalogue, then fan out one ``ingest_source`` task per source."""
    with _job(self, "ingestion.ingest_all", {"overrides": overrides}) as job:
        catalog = load_catalog()
        with session_factory()() as session, session.begin():
            sync = sync_catalog(session, catalog)
        slugs = [entry.slug for entry in catalog.source]
        task_ids = {slug: ingest_source.delay(slug, overrides).id for slug in slugs}
        job.record({"catalog_sync": sync, "dispatched": task_ids})
        return slugs


@app.task(name="embedding.embed_pending", queue="embedding", acks_late=True, bind=True)
def embed_pending_task(self: Task, target: str = "passage", limit: int = 200) -> dict[str, Any]:
    with _job(self, "embedding.embed_pending", {"target": target, "limit": limit}) as job:
        if target not in ("passage", "claim"):
            raise ValueError("target must be 'passage' or 'claim'")
        try:
            with default_embedder() as embedder:
                summary = asdict(embed_pending(session_factory(), embedder, target=target, limit=limit))
        except EmbedderUnavailable as exc:
            summary = {"target": target, "embedded": 0, "remaining": None, "errors": [str(exc)]}
        job.record(summary, problems=summary["errors"])
        log.info("Embedded %s: %s", target, summary)
        return summary
