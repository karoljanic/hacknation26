import asyncio
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from pydantic import HttpUrl

from prawdowid.adapters.sources.gov_api import CountryAdvisory, GovPlSearchClient, GovSearchHit, OdyseuszClient
from prawdowid.adapters.sources.wikipedia import WikipediaArticle, WikipediaClient
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
    # Names and number-bearing words are picked first, in their original order.
    assert queries[1] == "Wrocław 65-latkowie"
    assert "Wrocław jedynym miastem" in queries  # leading keywords
    assert all("Czy" not in query for query in queries[1:])
    assert len(set(queries)) == len(queries)


def test_search_queries_use_the_users_original_wording_too() -> None:
    queries = search_queries(
        "Wrocław był jedynym miastem w Polsce, gdzie osoby w wieku 65 lat jeździły za darmo.",
        source_quotes=["Czy Wrocław był jedynym miastem, w którym 65-latkowie jeździli za darmo?"],
    )

    assert queries[1].startswith("Czy Wrocław był jedynym miastem, w którym 65-latkowie")
    assert "Wrocław 65-latkowie" in queries
    assert len(queries) <= 10


def test_bare_numbers_rank_below_topic_words() -> None:
    queries = search_queries("Wrocław wydaje na komunikację około 700 mln zł.")

    assert queries[1] == "Wrocław komunikację"


def test_google_is_queried_with_the_claims_source_quotes() -> None:
    google = Mock(spec=FactCheckSource)
    google.search_text.return_value = google_page([])
    euvsdisinfo = Mock()
    euvsdisinfo.search = AsyncMock(return_value=euvsdisinfo_result([]))
    rewritten = ClaimForVerification(
        id="c1", text="Osoby w wieku 65 lat jeżdżą za darmo.", kind="factual_claim",
        source_quotes=["65-latkowie we Wrocławiu jeżdżą za darmo"],
    )

    asyncio.run(EvidenceSearchOrchestrator(google=google, euvsdisinfo=euvsdisinfo).search([rewritten]))

    sent = [call.args[0].query for call in google.search_text.call_args_list]
    assert "65-latkowie we Wrocławiu jeżdżą za darmo" in sent


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


def test_model_written_queries_are_sent_to_google_after_the_full_text() -> None:
    queries = search_queries("Wrocław był jedynym miastem.", ["bezpłatne przejazdy seniorzy Wrocław"])

    assert queries[:2] == ["Wrocław był jedynym miastem.", "bezpłatne przejazdy seniorzy Wrocław"]


def gov_hit(title: str, intro: str = "", url: str = "https://www.gov.pl/web/belgia/a") -> GovSearchHit:
    return GovSearchHit(title=title, intro=intro, url=url, section="belgia", published_at=None)


def test_gov_search_keeps_only_hits_sharing_most_query_terms() -> None:
    google = Mock(spec=FactCheckSource)
    google.search_text.return_value = google_page([])
    euvsdisinfo = Mock()
    euvsdisinfo.search = AsyncMock(return_value=euvsdisinfo_result([]))
    gov_search = Mock(spec=GovPlSearchClient)
    gov_search.search.return_value = [
        gov_hit("Dyrektor Generalny", url="https://www.gov.pl/web/wug/dyrektor-generalny"),
        gov_hit("Strajk generalny we Włochach 20 listopada", "Utrudnienia w transporcie.", "https://www.gov.pl/web/wlochy/strajk"),
    ]
    target = ClaimForVerification(
        id="c1", text="Czy we Włoszech był strajk generalny?", kind="verification_question",
        search_queries=["strajk generalny Włochy"],
    )

    result = asyncio.run(
        EvidenceSearchOrchestrator(google=google, euvsdisinfo=euvsdisinfo, gov_search=gov_search).search([target])
    )

    gov_items = [item for item in result.claims[0].items if item.source == "gov_pl_search"]
    assert [str(item.url) for item in gov_items] == ["https://www.gov.pl/web/wlochy/strajk"]
    assert gov_items[0].catalog_slug == "gov-pl-search"
    gov_search.search.assert_called_once_with("strajk generalny Włochy", size=10)


def advisory(code: str = "IT") -> CountryAdvisory:
    return CountryAdvisory(
        code=code, name="Włochy", page_url=f"https://odyseusz.gov.pl/{code}", updated_at=date(2026, 11, 19),
        notice="W dniu 20 listopada zapowiedziano strajk generalny.",
        warnings=["Poziom 1 - Zwykła ostrożność"], sections=[("Bezpieczeństwo", "Uważaj na kieszonkowców.")],
    )


def test_odyseusz_profiles_are_fetched_for_foreign_countries_only() -> None:
    google = Mock(spec=FactCheckSource)
    google.search_text.return_value = google_page([])
    euvsdisinfo = Mock()
    euvsdisinfo.search = AsyncMock(return_value=euvsdisinfo_result([]))
    odyseusz = Mock(spec=OdyseuszClient)
    odyseusz.country.return_value = advisory()
    target = ClaimForVerification(
        id="c1", text="Czy we Włoszech był strajk generalny?", kind="verification_question", countries=["PL", "IT"],
    )

    result = asyncio.run(
        EvidenceSearchOrchestrator(google=google, euvsdisinfo=euvsdisinfo, odyseusz=odyseusz).search([target])
    )

    odyseusz.country.assert_called_once_with("IT")
    items = [item for item in result.claims[0].items if item.source == "odyseusz"]
    assert items[0].snippet == "W dniu 20 listopada zapowiedziano strajk generalny."
    assert items[0].published_at == datetime(2026, 11, 19, tzinfo=timezone.utc)
    assert "Poziom 1" in items[1].snippet and "Bezpieczeństwo: Uważaj na kieszonkowców." in items[1].snippet


def test_odyseusz_outage_is_reported_not_hidden() -> None:
    google = Mock(spec=FactCheckSource)
    google.search_text.return_value = google_page([])
    euvsdisinfo = Mock()
    euvsdisinfo.search = AsyncMock(return_value=euvsdisinfo_result([]))
    odyseusz = Mock(spec=OdyseuszClient)
    odyseusz.country.side_effect = RuntimeError("timeout")
    target = ClaimForVerification(id="c1", text="Strajk we Włoszech.", kind="factual_claim", countries=["IT"])

    result = asyncio.run(
        EvidenceSearchOrchestrator(google=google, euvsdisinfo=euvsdisinfo, odyseusz=odyseusz).search([target])
    )

    assert result.claims[0].unavailable_sources == ["odyseusz"]


def test_wikipedia_articles_become_citable_tier3_items_and_outages_are_reported() -> None:
    google = Mock(spec=FactCheckSource)
    google.search_text.return_value = google_page([])
    euvsdisinfo = Mock()
    euvsdisinfo.search = AsyncMock(return_value=euvsdisinfo_result([]))
    wikipedia = Mock(spec=WikipediaClient)
    wikipedia.search.return_value = ["Bitwa pod Grunwaldem"]
    wikipedia.intros.return_value = [
        WikipediaArticle("Bitwa pod Grunwaldem", "https://pl.wikipedia.org/wiki/Bitwa_pod_Grunwaldem", "Stoczona 15 lipca 1410.")
    ]
    target = ClaimForVerification(
        id="c1", text="Kiedy była bitwa pod Grunwaldem?", kind="verification_question",
        search_queries=["bitwa pod Grunwaldem", "Grunwald 1410"],
    )
    orchestrator = EvidenceSearchOrchestrator(google=google, euvsdisinfo=euvsdisinfo, wikipedia=wikipedia)

    result = asyncio.run(orchestrator.search([target]))

    items = [item for item in result.claims[0].items if item.source == "wikipedia"]
    assert [(item.title, item.catalog_slug, item.snippet) for item in items] == [
        ("Bitwa pod Grunwaldem", "wikipedia-pl", "Stoczona 15 lipca 1410.")
    ]
    wikipedia.intros.assert_called_once_with(["Bitwa pod Grunwaldem"])

    wikipedia.search.side_effect = httpx.ConnectError("down")
    assert asyncio.run(orchestrator.search([target])).claims[0].unavailable_sources == ["wikipedia"]


def test_unconfigured_providers_are_reported_as_unavailable() -> None:
    euvsdisinfo = Mock()
    euvsdisinfo.search = AsyncMock(
        return_value=euvsdisinfo_result([{"type": "case", "title": "Case", "content": "Opis.", "link": "https://euvsdisinfo.eu/x"}])
    )

    result = asyncio.run(
        EvidenceSearchOrchestrator(google=None, euvsdisinfo=euvsdisinfo).search([claim("c1", "Rząd podniósł VAT.")])
    )

    assert result.claims[0].unavailable_sources == ["google_fact_check"]
    assert [item.source for item in result.claims[0].items] == ["euvsdisinfo"]

    result = asyncio.run(
        EvidenceSearchOrchestrator(google=None, euvsdisinfo=None).search([claim("c1", "Rząd podniósł VAT.")])
    )
    assert result.claims[0].unavailable_sources == ["google_fact_check", "euvsdisinfo"]


def test_euvsdisinfo_is_queried_in_english_when_a_translation_exists() -> None:
    google = Mock(spec=FactCheckSource)
    google.search_text.return_value = google_page([])
    euvsdisinfo = Mock()
    euvsdisinfo.search = AsyncMock(return_value=euvsdisinfo_result([]))
    claims = [
        ClaimForVerification(id="c1", text="Ukraina wystrzeliła rakietę w stronę Polski.", kind="factual_claim",
                             text_en="Ukraine fired a missile at Poland."),
        claim("c2", "Rząd podniósł VAT."),  # no translation: falls back to the original
    ]

    asyncio.run(EvidenceSearchOrchestrator(google=google, euvsdisinfo=euvsdisinfo).search(claims))

    queries = {call.args[0].query for call in euvsdisinfo.search.call_args_list}
    assert queries == {"Ukraine fired a missile at Poland.", "Rząd podniósł VAT."}
    # Google Fact Check (Polish fact-checkers) still gets the Polish text.
    assert "Ukraine fired a missile at Poland." not in {c.args[0].query for c in google.search_text.call_args_list}


def test_euvsdisinfo_transport_failure_does_not_cancel_the_rest_of_the_search() -> None:
    """Like the MCP client: requests run in a background task of a task group
    whose scope wraps the connection; a transport error there cancels the
    body and surfaces as an ExceptionGroup on exit (Test 3, DNS failure)."""
    from contextlib import asynccontextmanager

    import anyio
    import httpx

    @asynccontextmanager
    async def failing_connection():
        failed = anyio.Event()

        class Adapter:
            async def search(self, request):
                failed.set()
                await anyio.sleep(10)  # cancelled when the background task fails

        async def transport() -> None:
            await failed.wait()
            raise httpx.ConnectError("[Errno -5] No address associated with hostname")

        async with anyio.create_task_group() as group:
            group.start_soon(transport)
            yield Adapter()

    google = Mock(spec=FactCheckSource)
    google.search_text.return_value = google_page([("https://demagog.org.pl/a", "Analiza A")])
    claims = [claim("c1", "Rząd podniósł VAT."), claim("c2", "Inflacja spadła.")]

    result = asyncio.run(
        EvidenceSearchOrchestrator(google=google, euvsdisinfo_connect=failing_connection).search(claims)
    )

    for claim_evidence in result.claims:
        assert claim_evidence.unavailable_sources == ["euvsdisinfo"]
        assert [str(item.url) for item in claim_evidence.items] == ["https://demagog.org.pl/a"]


def test_euvsdisinfo_connection_is_opened_once_per_search() -> None:
    from contextlib import asynccontextmanager

    opened = []
    adapter = Mock()
    adapter.search = AsyncMock(return_value=euvsdisinfo_result(
        [{"type": "case", "title": "Case", "content": "Opis.", "link": "https://euvsdisinfo.eu/x"}]
    ))

    @asynccontextmanager
    async def connect():
        opened.append(1)
        yield adapter

    google = Mock(spec=FactCheckSource)
    google.search_text.return_value = google_page([])
    claims = [claim("c1", "Rząd podniósł VAT."), claim("c2", "Inflacja spadła.")]

    result = asyncio.run(EvidenceSearchOrchestrator(google=google, euvsdisinfo_connect=connect).search(claims))

    assert opened == [1]
    assert [[i.source for i in c.items] for c in result.claims] == [["euvsdisinfo"], ["euvsdisinfo"]]
