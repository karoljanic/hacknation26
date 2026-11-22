import json

import httpx
import pytest

from prawdowid.adapters.sources.gov_api import GovPlSearchClient, OdyseuszClient


def client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_gov_search_returns_absolute_gov_pl_urls_and_drops_foreign_hosts() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/data/search"
        assert request.url.params["query"] == "strajk"
        return httpx.Response(200, json={"count": 2, "results": [
            {"title": "Strajk w Belgii", "intro": "Utrudnienia.", "publishDate": "2026-10-06T08:00:00Z",
             "url": "/web/belgia/strajk", "path": "belgia"},
            {"title": "Obca strona", "intro": "", "url": "https://example.com/x", "path": None},
        ]})

    hits = GovPlSearchClient(client=client(handler)).search("strajk")

    assert [hit.url for hit in hits] == ["https://www.gov.pl/web/belgia/strajk"]
    assert hits[0].section == "belgia"
    assert hits[0].published_at is not None and hits[0].published_at.year == 2026


def test_odyseusz_profile_is_parsed_and_missing_country_is_none() -> None:
    profile = {
        "nazwa": "Włochy", "dataAktualizacji": "2026-11-19",
        "trescPowiadomieniaDluga": "W dniu 20 listopada zapowiedziano strajk generalny.",
        "ostrzezenia": [{"tresc": "Oznacza poziom zagrożenia zbliżony do Polski.",
                         "poziomZagrozenia": {"opis": "Poziom 1 - Zwykła ostrożność"},
                         "zakresTerytorialnyOstrzezenia": {"opis": "Bazowy dla kraju"}}],
        "sekcje": [{"zakresInformacyjny": {"opis": "Bezpieczeństwo"}, "podsumowanie": "- Uważaj na kieszonkowców."}],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/IT"):
            return httpx.Response(200, content=json.dumps(profile))
        return httpx.Response(204)

    odyseusz = OdyseuszClient(client=client(handler))
    advisory = odyseusz.country("it")

    assert advisory is not None
    assert advisory.page_url == "https://odyseusz.gov.pl/IT"
    assert advisory.notice == "W dniu 20 listopada zapowiedziano strajk generalny."
    assert advisory.warnings == ["Poziom 1 - Zwykła ostrożność – Bazowy dla kraju – Oznacza poziom zagrożenia zbliżony do Polski."]
    assert advisory.sections == [("Bezpieczeństwo", "- Uważaj na kieszonkowców.")]
    assert odyseusz.country("XX") is None


def test_odyseusz_rejects_non_iso_codes_before_any_request() -> None:
    with pytest.raises(ValueError):
        OdyseuszClient(client=client(lambda request: httpx.Response(500))).country("../x")
