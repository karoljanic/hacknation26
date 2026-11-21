"""IngestionService against real Postgres, inside a rolled-back transaction."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from prawdowid.domain.enums import IngestionStatus
from prawdowid.ingestion.catalog import Catalog, sync_catalog
from prawdowid.ingestion.domain import CollectorUnavailable, CorpusDocument, Skipped
from prawdowid.ingestion.service import IngestionService, domain_matches
from prawdowid.persistence.models import Document, IngestionRun, Passage

NOW = datetime(2026, 11, 21, 12, tzinfo=timezone.utc)
SLUG = "test-ingest"


def catalog(domain: str = "example.gov.pl") -> Catalog:
    return Catalog.model_validate(
        {
            "source": [
                {
                    "slug": SLUG,
                    "name": "Test",
                    "publisher": "Test",
                    "kind": "official",
                    "domain": domain,
                    "tier": 1,
                    "license": "test",
                    "inclusion_rule": "test",
                    "collect": {"collector": "fake", "batch": "default"},
                }
            ]
        }
    )


def doc(url: str, text: str, site: str | None = "example.gov.pl") -> CorpusDocument:
    return CorpusDocument(url=url, title=f"Tytuł {url}", text=text, fetched_at=NOW,
                          published_at=NOW, publisher_site=site, metadata={"k": "v"})


LONG = "To jest wystarczająco długa treść dokumentu urzędowego do zapisania w korpusie."


class FakeCollector:
    def __init__(self, batches: dict[str, list]) -> None:
        self.batches = batches

    def collect(self, params):
        for item in self.batches[params["batch"]]:
            if isinstance(item, Exception):
                raise item
            yield item


def make_service(sessions, batches=None, *, unavailable=False, cat=None) -> IngestionService:
    @contextmanager
    def open_collector(name: str) -> Iterator[FakeCollector]:
        assert name == "fake"
        if unavailable:
            raise CollectorUnavailable("FAKE_API_KEY is not set")
        yield FakeCollector(batches or {})

    cat = cat or catalog()
    with sessions() as session, session.begin():
        sync_catalog(session, cat)
    return IngestionService(session_factory=sessions, catalog=cat, open_collector=open_collector)


def test_domain_matching() -> None:
    assert domain_matches("www.demagog.org.pl", "demagog.org.pl")
    assert domain_matches("https://sprawdzam.afp.com/x", "sprawdzam.afp.com")
    assert domain_matches("konkret24.tvn24.pl", "tvn24.pl")
    assert not domain_matches("notdemagog.org.pl", "demagog.org.pl")
    assert not domain_matches("demagog.org.pl", "fakenews.pl")


def test_new_unchanged_and_changed_versions(isolated_sessions) -> None:
    batches = {
        "default": [doc("https://e.gov.pl/a", LONG), doc("https://e.gov.pl/b", LONG + " B")],
        "changed": [doc("https://e.gov.pl/a", LONG + " (aktualizacja)"), doc("https://e.gov.pl/b", LONG + " B")],
    }
    service = make_service(isolated_sessions, batches)

    first = service.ingest_source(SLUG)
    assert first.status is IngestionStatus.OK
    assert (first.counts["new"], first.counts["unchanged"]) == (2, 0)

    second = service.ingest_source(SLUG)
    assert (second.counts["new"], second.counts["unchanged"], second.counts["passages"]) == (0, 2, 0)

    third = service.ingest_source(SLUG, overrides={"batch": "changed"})
    assert (third.counts["new"], third.counts["unchanged"]) == (1, 1)

    with isolated_sessions() as session:
        versions = session.scalars(select(Document).where(Document.url == "https://e.gov.pl/a")).all()
        assert len(versions) == 2  # old version kept, new version added
        stored = versions[0]
        assert stored.meta == {"k": "v"} and stored.published_at == NOW
        passages = session.scalars(select(Passage).where(Passage.document_id == stored.id)).all()
        assert passages and all(p.embedding_model is None for p in passages)
        # Look runs up by id: inside the test's single transaction now() is
        # constant, so started_at cannot order them.
        runs = {r.id: r for r in session.scalars(select(IngestionRun).where(
            IngestionRun.id.in_([first.run_id, second.run_id, third.run_id])))}
        assert len(runs) == 3 and all(r.finished_at is not None for r in runs.values())
        assert runs[third.run_id].params == {"batch": "changed"}
        assert runs[first.run_id].params == {"batch": "default"}


def test_skips_and_rule_violations_are_counted_not_stored(isolated_sessions) -> None:
    batches = {
        "default": [
            Skipped(reason="listing failed", ref="https://e.gov.pl/x"),
            doc("https://e.gov.pl/short", "za krótko"),
            doc("https://other.pl/a", LONG, site="other.pl"),  # outside catalogued domain
            doc("https://e.gov.pl/ok", LONG),
        ]
    }
    result = make_service(isolated_sessions, batches).ingest_source(SLUG)
    assert result.status is IngestionStatus.OK
    assert result.counts == {"seen": 4, "new": 1, "unchanged": 0, "skipped": 3, "failed": 0, "passages": 1}
    assert any("listing failed" in e for e in result.errors)


def test_collector_crash_fails_run_but_keeps_earlier_documents(isolated_sessions) -> None:
    batches = {"default": [doc("https://e.gov.pl/a", LONG), RuntimeError("HTTP 503")]}
    result = make_service(isolated_sessions, batches).ingest_source(SLUG)
    assert result.status is IngestionStatus.FAILED
    assert result.counts["new"] == 1
    assert any("HTTP 503" in e for e in result.errors)
    with isolated_sessions() as session:
        assert session.scalar(select(Document.id).where(Document.url == "https://e.gov.pl/a"))
        assert session.get_one(IngestionRun, result.run_id).status is IngestionStatus.FAILED


def test_missing_key_is_a_skipped_run_not_an_empty_success(isolated_sessions) -> None:
    result = make_service(isolated_sessions, unavailable=True).ingest_source(SLUG)
    assert result.status is IngestionStatus.SKIPPED
    assert result.counts["seen"] == 0
    assert "FAKE_API_KEY" in result.errors[0]


def test_inactive_or_unsynced_source_is_refused(isolated_sessions) -> None:
    service = make_service(isolated_sessions)
    with isolated_sessions() as session, session.begin():
        sync_catalog(session, Catalog(source=[]))  # removes it -> inactive
    with pytest.raises(LookupError, match="inactive"):
        service.ingest_source(SLUG)
