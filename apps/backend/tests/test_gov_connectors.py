from unittest.mock import Mock

import pytest

from prawdowid.ai.adapters.decision import BooleanAnswer, DecisionResult
from prawdowid.ingestion.catalog import CollectSpec, SourceEntry
from prawdowid.ingestion.domain import CorpusDocument, Skipped
from prawdowid.services.gov_connectors import GovConnectorFetcher, GovConnectorSelector, _Document
from prawdowid.services.relevance import claim_terms


def entry(slug: str, *, listing_urls=("https://www.gov.pl/web/x/aktualnosci",)) -> SourceEntry:
    return SourceEntry(
        slug=slug, name=slug, publisher=f"Publisher {slug}", kind="official", domain="gov.pl",
        tier=1, license="test", inclusion_rule=f"Zakres {slug}",
        collect=CollectSpec(collector="gov_pl", listing_urls=list(listing_urls)),
    )


def decision_result(scores: list[float]) -> DecisionResult:
    return DecisionResult(
        model="test", input_tokens=1, output_tokens=1,
        answers={str(i): BooleanAnswer(type="noul", noul=score) for i, score in enumerate(scores)},
    )


def document(url: str = "https://www.gov.pl/web/x/a", title: str = "Komunikat") -> CorpusDocument:
    from datetime import datetime, timezone
    return CorpusDocument(url=url, title=title, text="Treść komunikatu." * 5, fetched_at=datetime.now(timezone.utc))


def test_selector_picks_sources_above_threshold_in_score_order() -> None:
    entries = [entry("gov-a"), entry("gov-b"), entry("gov-c")]
    decision = Mock()
    decision.decide.return_value = decision_result([0.9, 0.2, 0.6])

    selected = GovConnectorSelector(decision=decision, entries=entries, threshold=0.5).select("Teza o pogodzie.")

    assert [e.slug for e in selected] == ["gov-a", "gov-c"]


def test_selector_respects_max_sources() -> None:
    entries = [entry("gov-a"), entry("gov-b"), entry("gov-c")]
    decision = Mock()
    decision.decide.return_value = decision_result([0.9, 0.8, 0.7])

    selected = GovConnectorSelector(decision=decision, entries=entries, threshold=0.5, max_sources=2).select("Teza.")

    assert len(selected) == 2


def test_no_sources_above_threshold_returns_empty() -> None:
    entries = [entry("gov-a")]
    decision = Mock()
    decision.decide.return_value = decision_result([0.1])

    assert GovConnectorSelector(decision=decision, entries=entries, threshold=0.5).select("Teza.") == []


def test_empty_entries_are_rejected() -> None:
    with pytest.raises(ValueError, match="At least one"):
        GovConnectorSelector(decision=Mock(), entries=[])


def test_fetcher_maps_documents_to_evidence_items_with_exact_catalog_slug(monkeypatch) -> None:
    fake_collector = Mock()
    fake_collector.collect.return_value = iter([document(), Skipped(reason="no text", ref="x")])
    monkeypatch.setattr(
        "prawdowid.services.gov_connectors.GovPlCollector", Mock(return_value=fake_collector)
    )
    monkeypatch.setattr(
        "prawdowid.services.gov_connectors.GovPlHttpFetcher",
        Mock(return_value=Mock(__enter__=Mock(return_value=Mock()), __exit__=Mock(return_value=False))),
    )

    result = GovConnectorFetcher().fetch_many([entry("gov-rcb")])

    assert result.failed_slugs == []
    assert len(result.items) == 1
    item = result.items[0]
    assert item.source == "gov_pl"
    assert item.catalog_slug == "gov-rcb"
    assert item.publisher == "Publisher gov-rcb"


def test_fetcher_records_failed_slugs_without_losing_other_sources(monkeypatch) -> None:
    good_collector = Mock()
    good_collector.collect.return_value = iter([document()])
    bad_collector = Mock()
    bad_collector.collect.side_effect = RuntimeError("network down")
    collectors = iter([good_collector, bad_collector])
    monkeypatch.setattr(
        "prawdowid.services.gov_connectors.GovPlCollector", Mock(side_effect=lambda *_: next(collectors))
    )
    monkeypatch.setattr(
        "prawdowid.services.gov_connectors.GovPlHttpFetcher",
        Mock(return_value=Mock(__enter__=Mock(return_value=Mock()), __exit__=Mock(return_value=False))),
    )

    result = GovConnectorFetcher().fetch_many([entry("gov-good"), entry("gov-bad")])

    assert len(result.items) == 1
    assert result.failed_slugs == ["gov-bad"]


def site_entry(slug: str, collector: str = "official_page", **params) -> SourceEntry:
    return SourceEntry(
        slug=slug, name=slug, publisher=f"Publisher {slug}", kind="official", domain="drogi.gddkia.gov.pl",
        tier=1, license="test", inclusion_rule=f"Zakres {slug}", collect=CollectSpec(collector=collector, **params),
    )


def test_official_page_keeps_only_the_passages_about_the_claim() -> None:
    page = _Document(
        title="Lista utrudnień",
        url="https://drogi.gddkia.gov.pl/lista",
        text="\n".join(
            [f"Utrudnienie {n} na drodze krajowej nr {n} w województwie mazowieckim." for n in range(1, 60)]
            + ["Prace drogowe na drodze S61 między węzłem Śniadowo a węzłem Szczuczyn do 30 listopada."]
        ),
    )
    reader = Mock(return_value=page)
    fetcher = GovConnectorFetcher(page_reader=reader)

    result = fetcher.fetch_many(
        [site_entry("gddkia-drogi", urls=["https://drogi.gddkia.gov.pl/lista"])],
        claim_terms(["Prace drogowe na S61 między Śniadowem a Szczuczynem"]),
    )

    reader.assert_called_once_with("https://drogi.gddkia.gov.pl/lista", "drogi.gddkia.gov.pl")
    [item] = result.items
    assert item.source == "official_site" and item.catalog_slug == "gddkia-drogi"
    assert "Szczuczyn" in item.snippet and len(item.snippet) < 1000


def test_measurement_api_without_matching_documents_contributes_nothing() -> None:
    station = _Document(title="Synop: Hel", url="https://danepubliczne.imgw.pl/api/data/synop", text="Hel temperatura 3,4")
    fetcher = GovConnectorFetcher(imgw_reader=lambda: [station])

    matched = fetcher.fetch_many([site_entry("imgw", collector="imgw")], claim_terms(["Temperatura na Helu"]))
    unrelated = fetcher.fetch_many([site_entry("imgw", collector="imgw")], claim_terms(["Strajk nauczycieli"]))

    assert [item.source for item in matched.items] == ["imgw"]
    assert unrelated.items == [] and unrelated.failed_slugs == []
