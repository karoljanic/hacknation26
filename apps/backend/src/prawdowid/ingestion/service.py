"""Run one collector for one catalogued source and store what it yields.

* idempotent: a document is identified by (source, url, content_hash); an
  unchanged page is counted, not stored again; changed text at the same URL
  becomes a new version;
* per-document isolation: each document is committed on its own, so one bad
  item never discards the others;
* every run is recorded in ``ingestion_run`` (status, counts, errors), including
  runs that were skipped (missing key) or failed (source down). An empty or
  failed run is never mistaken for "the source has no documents".
"""

import logging
import uuid
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from prawdowid.adapters.sources.gov_pl import content_hash, normalize_text
from prawdowid.domain.enums import IngestionStatus
from prawdowid.ingestion.catalog import Catalog, SourceEntry
from prawdowid.ingestion.chunking import chunk_text
from prawdowid.ingestion.collectors.registry import open_collector as default_open_collector
from prawdowid.ingestion.domain import Collector, CollectorUnavailable, CorpusDocument, Skipped
from prawdowid.persistence.models import Document, IngestionRun, Passage, Source

log = logging.getLogger(__name__)

MIN_TEXT_CHARS = 40
MAX_LOGGED_ERRORS = 50


@dataclass
class IngestionResult:
    slug: str
    run_id: uuid.UUID | None
    status: IngestionStatus
    counts: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


def describe_error(exc: BaseException) -> str:
    """Readable one-liner; unwraps (nested) exception groups from anyio/MCP."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return f"{type(exc).__name__}: {exc}"


def _bare_host(site: str) -> str:
    site = site.strip().lower()
    if "://" in site:
        site = site.split("://", 1)[1]
    site = site.split("/", 1)[0]
    return site.removeprefix("www.")


def domain_matches(site: str, domain: str) -> bool:
    site, domain = _bare_host(site), _bare_host(domain)
    return site == domain or site.endswith("." + domain)


class IngestionService:
    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        catalog: Catalog,
        open_collector: Callable[[str], AbstractContextManager[Collector]] = default_open_collector,
        min_text_chars: int = MIN_TEXT_CHARS,
    ) -> None:
        self.session_factory = session_factory
        self.catalog = catalog
        self.open_collector = open_collector
        self.min_text_chars = min_text_chars

    def ingest_all(
        self, *, overrides: Mapping[str, Any] | None = None, job_id: uuid.UUID | None = None
    ) -> list[IngestionResult]:
        return [
            self.ingest_source(entry.slug, overrides=overrides, job_id=job_id)
            for entry in self.catalog.source
        ]

    def ingest_source(
        self,
        slug: str,
        *,
        overrides: Mapping[str, Any] | None = None,
        job_id: uuid.UUID | None = None,
    ) -> IngestionResult:
        """``job_id`` links the ``ingestion_run`` to the job_run that performed it."""
        entry = self.catalog.get(slug)
        params = {**entry.collect.params, **(overrides or {})}
        source_id, run_id = self._open_run(entry, params, job_id)
        counts = {"seen": 0, "new": 0, "unchanged": 0, "skipped": 0, "failed": 0, "passages": 0}
        errors: list[str] = []

        def note(message: str) -> None:
            if len(errors) < MAX_LOGGED_ERRORS:
                errors.append(message)

        status = IngestionStatus.OK
        try:
            with self.open_collector(entry.collect.collector) as collector:
                for item in collector.collect(params):
                    counts["seen"] += 1
                    if isinstance(item, Skipped):
                        counts["skipped"] += 1
                        note(f"skipped {item.ref or '-'}: {item.reason}")
                        continue
                    try:
                        outcome, passages = self._store(source_id, entry, item)
                    except Exception as exc:  # one bad document must not stop the run
                        log.warning("Storing %s failed: %s", item.url, describe_error(exc))
                        log.debug("Storage traceback", exc_info=True)
                        counts["failed"] += 1
                        note(f"failed {item.url}: {describe_error(exc)}")
                        continue
                    counts[outcome] += 1
                    counts["passages"] += passages
                    if outcome == "skipped":
                        note(f"skipped {item.url}: outside catalogue rules")
        except CollectorUnavailable as exc:
            status = IngestionStatus.SKIPPED
            note(f"collector unavailable: {exc}")
        except Exception as exc:
            log.warning("Collector for %s failed: %s", slug, describe_error(exc))
            log.debug("Collector traceback", exc_info=True)
            status = IngestionStatus.FAILED
            note(f"collector failed: {describe_error(exc)}")
        else:
            if counts["failed"]:
                status = IngestionStatus.PARTIAL

        self._close_run(run_id, status, counts, errors)
        return IngestionResult(slug, run_id, status, counts, errors)

    # -- storage ------------------------------------------------------------ #

    def _open_run(
        self, entry: SourceEntry, params: Mapping[str, Any], job_id: uuid.UUID | None
    ) -> tuple[uuid.UUID, uuid.UUID]:
        with self.session_factory() as session, session.begin():
            source = session.scalars(select(Source).where(Source.slug == entry.slug)).first()
            if source is None:
                raise LookupError(f"Source '{entry.slug}' is not in the database; run sync-catalog first")
            if not source.active:
                raise LookupError(f"Source '{entry.slug}' is inactive (excluded from the catalogue)")
            run = IngestionRun(
                source_id=source.id, status=IngestionStatus.RUNNING, params=dict(params), job_id=job_id
            )
            session.add(run)
            session.flush()
            return source.id, run.id

    def _close_run(
        self, run_id: uuid.UUID, status: IngestionStatus, counts: dict[str, int], errors: list[str]
    ) -> None:
        with self.session_factory() as session, session.begin():
            run = session.get_one(IngestionRun, run_id)
            run.status = status
            run.finished_at = datetime.now(timezone.utc)
            run.counts = counts
            run.errors = errors

    def _store(self, source_id: uuid.UUID, entry: SourceEntry, doc: CorpusDocument) -> tuple[str, int]:
        """Returns ("new" | "unchanged" | "skipped", number of passages stored)."""
        if doc.publisher_site and not domain_matches(doc.publisher_site, entry.domain):
            return "skipped", 0
        text = normalize_text(doc.text)
        if len(text) < self.min_text_chars:
            return "skipped", 0
        digest = content_hash(text)
        with self.session_factory() as session:
            exists = session.scalar(
                select(Document.id).where(
                    Document.source_id == source_id,
                    Document.url == doc.url,
                    Document.content_hash == digest,
                )
            )
            if exists is not None:
                return "unchanged", 0
            chunks = chunk_text(text)
            document = Document(
                source_id=source_id,
                url=doc.url,
                title=normalize_text(doc.title) or doc.url,
                published_at=doc.published_at,
                fetched_at=doc.fetched_at,
                content_hash=digest,
                lang=doc.lang,
                meta=dict(doc.metadata),
                passages=[
                    Passage(
                        ordinal=chunk.ordinal,
                        text=chunk.text,
                        char_start=chunk.char_start,
                        char_end=chunk.char_end,
                    )
                    for chunk in chunks
                ],
            )
            session.add(document)
            try:
                session.commit()
            except IntegrityError:  # stored concurrently by another worker
                session.rollback()
                return "unchanged", 0
            return "new", len(chunks)
