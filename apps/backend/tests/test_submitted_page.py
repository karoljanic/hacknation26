from datetime import datetime, timezone

from prawdowid.adapters.sources.web import FetchedArticle
from prawdowid.ingestion.catalog import CollectSpec, SourceEntry
from prawdowid.services.gov_connectors import SourceDocument
from prawdowid.services.relevance import claim_terms
from prawdowid.services.submitted_page import SubmittedPageEvidence, trusted_site


def entry(slug: str, domain: str, collector: str = "official_page", kind: str = "official") -> SourceEntry:
    return SourceEntry(
        slug=slug, name=slug, publisher=f"Wydawca {slug}", kind=kind, domain=domain, tier=1, license="test",
        inclusion_rule="test", collect=CollectSpec(collector=collector),
    )


ENTRIES = [
    entry("gov-pl-search", "gov.pl", collector="live_only"),
    entry("gov-rcb", "gov.pl", collector="gov_pl"),
    entry("ulc", "ulc.gov.pl"),
    entry("euvsdisinfo", "euvsdisinfo.eu", collector="euvsdisinfo", kind="fact_checker"),
]


def test_only_catalogued_sites_are_trusted_and_gov_pl_maps_to_its_own_entry() -> None:
    assert trusted_site("https://www.gov.pl/web/poradnikbezpieczenstwa", ENTRIES).slug == "gov-pl-search"
    assert trusted_site("https://ulc.gov.pl/prawa-pasazera", ENTRIES).slug == "ulc"
    assert trusted_site("https://portal-plotkarski.pl/news", ENTRIES) is None
    assert trusted_site("https://euvsdisinfo.eu/report/x", ENTRIES) is None


def test_submitted_page_and_its_best_matching_subpage_become_evidence() -> None:
    landing = (
        b"<html><body><main>"
        b"<a href='https://www.gov.pl/web/poradnikbezpieczenstwa/pozar'>Pozar gasnica</a>"
        b"<a href='https://www.gov.pl/web/poradnikbezpieczenstwa/plan-na-kryzys'>Plan na kryzys zapasy wody</a>"
        b"</main></body></html>"
    )
    read: list[str] = []

    def reader(url: str, domain: str) -> SourceDocument:
        read.append(url)
        return SourceDocument(title="Plan na kryzys", url=url, text="Zgromadź zapas wody pitnej na co najmniej 3 dni.")

    evidence = SubmittedPageEvidence(
        entries=ENTRIES,
        link_fetcher=lambda url: FetchedArticle(url=url, body=landing, fetched_at=datetime.now(timezone.utc)),
        page_reader=reader,
    )
    wanted = claim_terms(["Czy poradnik zaleca zapas wody pitnej?"])

    items = evidence.items_for(
        "https://www.gov.pl/web/poradnikbezpieczenstwa", "Poradnik bezpieczeństwa. Przygotuj zapasy wody.", {"c1": wanted}
    )["c1"]

    assert read == ["https://www.gov.pl/web/poradnikbezpieczenstwa/plan-na-kryzys"]
    assert [item.source for item in items] == ["submitted_page", "submitted_page"]
    assert all(item.catalog_slug == "gov-pl-search" for item in items)
    assert "zapas wody pitnej" in items[1].snippet
    assert evidence.items_for("https://portal-plotkarski.pl/x", "Zapas wody.", {"c1": wanted}) == {}
