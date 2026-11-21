import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import HttpUrl

from prawdowid.domain.evidence import ClaimForVerification, EvidenceItem
from prawdowid.domain.euvsdisinfo import EUvsDisinfoResult
from prawdowid.domain.fact_checks import FactCheckClaim, FactCheckPage, FactCheckPublisher, FactCheckReview, FactCheckSource
from prawdowid.services.evidence_search import EvidenceSearchOrchestrator, search_queries
from prawdowid.services.gov_connectors import GovFetchResult


def claim(claim_id: str, text: str, kind: str = "factual_claim") -> ClaimForVerification:
    return ClaimForVerification(id=claim_id, text=text, kind=kind)


def google_page(reviews: list[tuple[str, str]]) -> FactCheckPage:
    return FactCheckPage(
        claims=[
            FactCheckClaim(
                text="Checked claim.",
                reviews=[
                    FactCheckReview(
                        url=HttpUrl(url), publisher=FactCheckPublisher(name="Demagog", site="demagog.org.pl"),
                        title=title, textual_rating="Fałsz", reviewed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                    )
                    for url, title in reviews
                ],
            )
        ],
        next_page_token=None, fetched_at=datetime.now(timezone.utc),
    )


def euvsdisinfo_result(results: list[dict]) -> EUvsDisinfoResult:
    return EUvsDisinfoResult(
        tool="search", content=[], structured_content={"results": results}, fetched_at=datetime.now(timezone.utc)
    )


def test_both_providers_are_queried_concurrently_for_every_non_opinion_claim() -> None:
    google = Mock(spec=FactCheckSource)
    google.search_text.return_value = google_page([("https://demagog.org.pl/a", "Analiza A")])
    euvsdisinfo = Mock()
    euvsdisinfo.search = AsyncMock(
        return_value=euvsdisinfo_result([{"type": "article", "title": "Case", "content": "Opis.", "link": "https://euvsdisinfo.eu/x"}])
    )
    claims = [
        claim("c1", "Rząd podniósł VAT."),
        claim("c2", "To moim zdaniem błąd.", kind="opinion"),
    ]

    result = asyncio.run(EvidenceSearchOrchestrator(google=google, euvsdisinfo=euvsdisinfo).search(claims))

    assert result.skipped_opinion_ids == ["c2"]
    assert len(result.claims) == 1
    sources = {item.source for item in result.claims[0].items}
    assert sources == {"google_fact_check", "euvsdisinfo"}
    assert google.search_text.call_args_list[0].args[0].query == "Rząd podniósł VAT."


def test_search_queries_add_short_keyword_variants_without_question_words() -> None:
    queries = search_queries("Czy Wrocław był jedynym miastem w Polsce, w którym 65-latkowie jeździli za darmo?")

    assert queries[0].startswith("Czy Wrocław był")
    assert "Wrocław jedynym" in queries
    assert "Wrocław jedynym miastem Polsce 65-latkowie" in queries
    assert all("Czy" not in query for query in queries[1:])
    assert len(set(queries)) == len(queries)


def test_google_variants_are_merged_by_url_and_survive_partial_failure() -> None:
    google = Mock(spec=FactCheckSource)
    hit = google_page([("https://demagog.org.pl/a", "Analiza A")])
    google.search_text.side_effect = [RuntimeError("one query failed"), hit, hit, hit, hit, hit]
    euvsdisinfo = Mock()
    euvsdisinfo.search = AsyncMock(return_value=euvsdisinfo_result([]))
    claims = [claim("c1", "Wrocław był jedynym miastem w Polsce, gdzie seniorzy jeździli za darmo.")]

    result = asyncio.run(EvidenceSearchOrchestrator(google=google, euvsdisinfo=euvsdisinfo).search(claims))

    google_items = [item for item in result.claims[0].items if item.source == "google_fact_check"]
    assert [str(item.url) for item in google_items] == ["https://demagog.org.pl/a"]
    assert result.claims[0].unavailable_sources == []


def test_one_provider_failure_does_not_lose_the_others_results() -> None:
    google = Mock(spec=FactCheckSource)
    google.search_text.side_effect = RuntimeError("Google is down")
    euvsdisinfo = Mock()
    euvsdisinfo.search = AsyncMock(
        return_value=euvsdisinfo_result([{"type": "case", "title": "Case", "content": "Opis.", "link": "https://euvsdisinfo.eu/x"}])
    )
    claims = [claim("c1", "Rząd podniósł VAT.")]

    result = asyncio.run(EvidenceSearchOrchestrator(google=google, euvsdisinfo=euvsdisinfo).search(claims))

    assert result.claims[0].unavailable_sources == ["google_fact_check"]
    assert [item.source for item in result.claims[0].items] == ["euvsdisinfo"]


def test_malformed_euvsdisinfo_shape_is_reported_as_an_error_not_fabricated() -> None:
    google = Mock(spec=FactCheckSource)
    google.search_text.return_value = google_page([])
    euvsdisinfo = Mock()
    euvsdisinfo.search = AsyncMock(
        return_value=EUvsDisinfoResult(
            tool="search", content=[], structured_content=None, fetched_at=datetime.now(timezone.utc)
        )
    )
    claims = [claim("c1", "Rząd podniósł VAT.")]

    result = asyncio.run(EvidenceSearchOrchestrator(google=google, euvsdisinfo=euvsdisinfo).search(claims))

    assert result.claims[0].unavailable_sources == ["euvsdisinfo"]


def test_duplicate_claim_ids_are_rejected() -> None:
    claims = [claim("c1", "A"), claim("c1", "B")]
    with pytest.raises(ValueError, match="unique"):
        asyncio.run(
            EvidenceSearchOrchestrator(google=Mock(spec=FactCheckSource), euvsdisinfo=Mock()).search(claims)
        )


def test_too_many_claims_is_rejected() -> None:
    claims = [claim(f"c{i}", f"Teza {i}") for i in range(11)]
    with pytest.raises(ValueError, match="limit"):
        asyncio.run(
            EvidenceSearchOrchestrator(
                google=Mock(spec=FactCheckSource), euvsdisinfo=Mock(), max_claims=10
            ).search(claims)
        )


def _no_hit_google_euvsdisinfo() -> tuple[Mock, Mock]:
    google = Mock(spec=FactCheckSource)
    google.search_text.return_value = google_page([])
    euvsdisinfo = Mock()
    euvsdisinfo.search = AsyncMock(
        return_value=euvsdisinfo_result([])
    )
    return google, euvsdisinfo


def gov_item(slug: str = "gov-rcb") -> EvidenceItem:
    return EvidenceItem(
        source="gov_pl", kind="official", title="Komunikat RCB", url="https://www.gov.pl/web/rcb/a",
        publisher="RCB", catalog_slug=slug, snippet="Treść komunikatu.",
    )


def test_gov_selector_items_are_added_when_configured() -> None:
    google, euvsdisinfo = _no_hit_google_euvsdisinfo()
    gov_selector = Mock()
    gov_selector.select.return_value = ["gov-rcb"]
    gov_fetcher = Mock()
    gov_fetcher.fetch_many.return_value = GovFetchResult(items=[gov_item()], failed_slugs=[])
    claims = [claim("c1", "Czy ogłoszono alarm powodziowy?")]

    result = asyncio.run(
        EvidenceSearchOrchestrator(
            google=google, euvsdisinfo=euvsdisinfo, gov_selector=gov_selector, gov_fetcher=gov_fetcher
        ).search(claims)
    )

    assert [item.source for item in result.claims[0].items] == ["gov_pl"]
    gov_selector.select.assert_called_once_with("Czy ogłoszono alarm powodziowy?")


def test_gov_selector_disabled_by_default() -> None:
    google, euvsdisinfo = _no_hit_google_euvsdisinfo()
    claims = [claim("c1", "Teza.")]

    result = asyncio.run(EvidenceSearchOrchestrator(google=google, euvsdisinfo=euvsdisinfo).search(claims))

    assert result.claims[0].items == []


def test_gov_fetch_failure_degrades_gracefully_without_marking_claim_unavailable() -> None:
    google, euvsdisinfo = _no_hit_google_euvsdisinfo()
    gov_selector = Mock()
    gov_selector.select.return_value = ["gov-rcb"]
    gov_fetcher = Mock()
    gov_fetcher.fetch_many.side_effect = RuntimeError("network down")
    claims = [claim("c1", "Teza.")]

    result = asyncio.run(
        EvidenceSearchOrchestrator(
            google=google, euvsdisinfo=euvsdisinfo, gov_selector=gov_selector, gov_fetcher=gov_fetcher
        ).search(claims)
    )

    assert result.claims[0].items == []
    assert result.claims[0].unavailable_sources == []


def test_gov_selection_failure_degrades_gracefully() -> None:
    google, euvsdisinfo = _no_hit_google_euvsdisinfo()
    gov_selector = Mock()
    gov_selector.select.side_effect = RuntimeError("decision adapter down")

    result = asyncio.run(
        EvidenceSearchOrchestrator(
            google=google, euvsdisinfo=euvsdisinfo, gov_selector=gov_selector
        ).search([claim("c1", "Teza.")])
    )

    assert result.claims[0].items == []
    assert result.claims[0].unavailable_sources == []
