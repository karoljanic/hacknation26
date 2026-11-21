import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, Mock

import httpx
import pytest
from pydantic import ValidationError

from prawdowid.adapters.sources.google_fact_check import FactCheckAPIError, GoogleFactCheckAdapter
from prawdowid.domain.claims import Claim, ClaimExtraction, ClaimReference
from prawdowid.ai.adapters.llm import LLMAdapter, StructuredResult
from prawdowid.domain.fact_checks import (
    FactCheckPage,
    FactCheckSource,
    ImageSearchRequest,
    TextSearchRequest,
)
from prawdowid.services.fact_checks import FactCheckTool

GOOGLE_CLAIM = {
    "text": "Przykładowa teza.",
    "claimant": "Przykładowy autor",
    "claimDate": "2025-01-01T00:00:00Z",
    "claimReview": [
        {
            "publisher": {"name": "Redakcja", "site": "example.org"},
            "url": "https://example.org/fact-check",
            "title": "Analiza tezy",
            "reviewDate": "2025-01-02T00:00:00Z",
            "textualRating": "Misleading",
            "languageCode": "pl",
        }
    ],
}


def test_text_search_sends_filters_and_keeps_publisher_rating() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1alpha1/claims:search"
        assert request.headers["X-Goog-Api-Key"] == "test-key"
        assert "key" not in request.url.params
        assert not request.content
        assert dict(request.url.params) == {
            "query": "Przykładowa teza.",
            "languageCode": "pl",
            "pageSize": "5",
            "pageToken": "page-2",
            "reviewPublisherSiteFilter": "example.org",
            "maxAgeDays": "30",
        }
        return httpx.Response(200, json={"claims": [GOOGLE_CLAIM], "nextPageToken": "page-3"})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        page = GoogleFactCheckAdapter(client=client, api_key="test-key").search_text(
            TextSearchRequest(
                query="Przykładowa teza.",
                publisher_site="example.org",
                language_code="pl",
                page_size=5,
                page_token="page-2",
                max_age_days=30,
            )
        )
    review = page.claims[0].reviews[0]
    assert review.textual_rating == "Misleading"
    assert review.publisher.name == "Redakcja"
    assert review.reviewed_at == datetime(2025, 1, 2, tzinfo=timezone.utc)
    assert page.next_page_token == "page-3"
    assert page.fetched_at.tzinfo is not None


def test_image_search_unwraps_image_results() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1alpha1/claims:imageSearch"
        assert dict(request.url.params) == {
            "imageUri": "https://example.org/image.png",
            "pageSize": "10",
            "offset": "10",
        }
        return httpx.Response(
            200, json={"results": [{"claim": GOOGLE_CLAIM}], "nextPageToken": "next"}
        )

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        page = GoogleFactCheckAdapter(client=client, api_key="test-key").search_image(
            ImageSearchRequest(image_url="https://example.org/image.png", offset=10)
        )
    assert page.claims[0].text == GOOGLE_CLAIM["text"]
    assert page.next_page_token == "next"


def test_publisher_search_without_query_and_empty_results_are_valid() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert "query" not in request.url.params
        assert request.url.params["reviewPublisherSiteFilter"] == "example.org"
        assert request.url.params["pageSize"] == "100"
        return httpx.Response(200, json={})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        page = GoogleFactCheckAdapter(client=client, api_key="test-key").search_text(
            TextSearchRequest(publisher_site="example.org", page_size=100)
        )
    assert page.claims == []
    assert page.next_page_token is None


def test_malformed_image_result_is_not_empty_success() -> None:
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"results": [{}]})
        )
    ) as client:
        with pytest.raises(RuntimeError, match="invalid image search"):
            GoogleFactCheckAdapter(client=client, api_key="test-key").search_image(
                ImageSearchRequest(image_url="https://example.org/image.png")
            )


@pytest.mark.parametrize("status", [302, 401, 403, 429, 500])
def test_api_failures_are_not_empty_success_and_redirects_do_not_leak_key(status: int) -> None:
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            status, headers={"Retry-After": "60", "Location": "https://other.example/"}
        )

    with httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=True) as client:
        with pytest.raises(FactCheckAPIError) as raised:
            GoogleFactCheckAdapter(client=client, api_key="test-key").search_text(
                TextSearchRequest(query="teza")
            )
    assert raised.value.status_code == status
    assert raised.value.retry_after == "60"
    assert "test-key" not in str(raised.value)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "payload",
    [
        b"not JSON",
        json.dumps({"claims": "wrong"}).encode(),
        json.dumps({"error": {"message": "failed"}}).encode(),
        json.dumps({"claims": [{"text": "teza", "claimReview": [{"url": "not a URL"}]}]}).encode(),
    ],
)
def test_malformed_api_responses_are_explicit_errors(payload: bytes) -> None:
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=payload))
    ) as client:
        with pytest.raises(RuntimeError, match="invalid text search"):
            GoogleFactCheckAdapter(client=client, api_key="test-key").search_text(
                TextSearchRequest(query="teza")
            )


@pytest.mark.parametrize("size", [2_000_000, 2_000_001])
def test_response_size_limit_is_exact(size: int) -> None:
    payload = b"{}" + b" " * (size - 2)
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=payload))
    ) as client:
        source = GoogleFactCheckAdapter(client=client, api_key="test-key")
        if size == 2_000_001:
            with pytest.raises(RuntimeError, match="2 MB limit"):
                source.search_text(TextSearchRequest(query="teza"))
        else:
            assert source.search_text(TextSearchRequest(query="teza")).claims == []


@pytest.mark.parametrize(
    "options",
    [
        {},
        {"query": " "},
        {"query": "teza", "page_size": 0},
        {"query": "teza", "page_size": 101},
        {"query": "teza", "page_token": "next", "offset": 0},
        {"query": "teza", "max_age_days": -1},
    ],
)
def test_invalid_search_options_are_rejected(options: dict) -> None:
    with pytest.raises(ValidationError):
        TextSearchRequest.model_validate(options)


@pytest.mark.parametrize("url", ["file:///image.png", "data:image/png;base64,abc", "https://a:b@example.org/i.png"])
def test_image_search_requires_http_url_without_credentials(url: str) -> None:
    with pytest.raises(ValidationError):
        ImageSearchRequest(image_url=url)


def claim(id: str, text: str, *, opinion: bool = False, segment_id: str = "s1") -> Claim:
    return Claim(
        id=id,
        text=text,
        kind="opinion" if opinion else "factual_claim",
        references=[ClaimReference(segment_id=segment_id, quote=text)],
        ambiguities=[],
    )


def test_claim_tool_deduplicates_queries_and_reports_skipped_opinions() -> None:
    source = Mock(spec=FactCheckSource)
    source.search_text.return_value = FactCheckPage(
        claims=[], next_page_token=None, fetched_at=datetime.now(timezone.utc)
    )
    result = FactCheckTool(source).search_claims(
        ClaimExtraction(
            claims=[claim("c1", "Teza."), claim("c2", "Teza."), claim("c3", "Opinia.", opinion=True)]
        )
    )
    source.search_text.assert_called_once_with(
        TextSearchRequest(query="Teza.", language_code="pl", page_size=5)
    )
    assert [match.claim_id for match in result.matches] == ["c1", "c2"]
    assert result.skipped_opinion_ids == ["c3"]


def test_claim_query_limit_fails_before_making_requests() -> None:
    source = Mock(spec=FactCheckSource)
    with pytest.raises(ValueError, match="query limit"):
        FactCheckTool(source).search_claims(
            ClaimExtraction(claims=[claim("c1", "Teza 1."), claim("c2", "Teza 2.")]),
            max_queries=1,
        )
    source.search_text.assert_not_called()


@pytest.mark.parametrize("enabled", [False, True])
def test_claim_cli_fact_check_is_opt_in_and_preserves_claim_links(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], enabled: bool
) -> None:
    path = Path(__file__).parents[1] / "examples" / "extract_claims.py"
    spec = importlib.util.spec_from_file_location("extract_claims_demo", path)
    assert spec is not None and spec.loader is not None
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)
    llm = Mock(spec=LLMAdapter)
    llm.generate_structured.return_value = StructuredResult(
        data=ClaimExtraction(claims=[claim("c1", "Teza.", segment_id="input-1")]),
        requested_model="test",
        model_version="v1",
        input_tokens=10,
        output_tokens=10,
    )
    monkeypatch.setattr(demo, "GeminiAdapter", Mock(return_value=llm))
    monkeypatch.setattr(demo.genai, "Client", Mock(return_value=MagicMock()))
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("FACTCHECK_API_KEY", "test-fact-check-key")
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"claims": [GOOGLE_CLAIM]})

    http_client = httpx.Client(transport=httpx.MockTransport(handle))
    factory = Mock(return_value=http_client)
    monkeypatch.setattr(demo.httpx, "Client", factory)
    argv = ["extract_claims.py", "--text", "Teza."]
    if enabled:
        argv.append("--fact-check")
    monkeypatch.setattr("sys.argv", argv)
    try:
        demo.main()
    finally:
        http_client.close()
    output = json.loads(capsys.readouterr().out)
    assert output["extraction"]["claims"][0]["id"] == "c1"
    if enabled:
        assert output["fact_checks"]["matches"][0]["claim_id"] == "c1"
        assert len(requests) == 1
        assert requests[0].headers["X-Goog-Api-Key"] == "test-fact-check-key"
    else:
        assert "fact_checks" not in output
        factory.assert_not_called()
