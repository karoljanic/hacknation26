"""Celery tasks for corpus ingestion and embedding.

The tasks are independent: ingestion only writes documents/passages, embedding
only fills rows whose vector is NULL. Neither enqueues the other, so they can be
scheduled, retried and scaled separately.
"""

from dataclasses import asdict
from typing import Any

from prawdowid.ingestion.catalog import load_catalog, sync_catalog
from prawdowid.ingestion.service import IngestionService
from prawdowid.persistence.db import session_factory
from prawdowid.services.embedding import EmbedderUnavailable, default_embedder, embed_pending
from prawdowid.workers.celery_app import app


@app.task(name="ingestion.ingest_source", queue="ingestion", acks_late=True)
def ingest_source(slug: str, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    catalog = load_catalog()
    service = IngestionService(session_factory=session_factory(), catalog=catalog)
    result = service.ingest_source(slug, overrides=overrides)
    return {"slug": slug, "run_id": str(result.run_id), "status": result.status.value, **result.counts}


@app.task(name="ingestion.ingest_all", queue="ingestion")
def ingest_all(overrides: dict[str, Any] | None = None) -> list[str]:
    """Sync the catalogue, then fan out one ``ingest_source`` task per source."""
    catalog = load_catalog()
    with session_factory()() as session, session.begin():
        sync_catalog(session, catalog)
    for entry in catalog.source:
        ingest_source.delay(entry.slug, overrides)
    return [entry.slug for entry in catalog.source]


@app.task(name="embedding.embed_pending", queue="embedding", acks_late=True)
def embed_pending_task(target: str = "passage", limit: int = 200) -> dict[str, Any]:
    if target not in ("passage", "claim"):
        raise ValueError("target must be 'passage' or 'claim'")
    try:
        with default_embedder() as embedder:
            return asdict(embed_pending(session_factory(), embedder, target=target, limit=limit))
    except EmbedderUnavailable as exc:
        return {"target": target, "embedded": 0, "errors": [str(exc)]}
