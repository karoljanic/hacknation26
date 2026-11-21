import json
from unittest.mock import Mock

import httpx
import pytest
from google import genai
from google.genai import types

from prawdowid.ai.adapters.llm import LLMAdapter, StructuredResult
from prawdowid.ai.instances.gemini import GeminiAdapter
from prawdowid.domain.claims import Claim, ClaimExtraction, ClaimReference, InputSegment
from prawdowid.services.claims import ClaimExtractor


def extraction_result(claims: list[Claim]) -> StructuredResult[ClaimExtraction]:
    return StructuredResult(
        data=ClaimExtraction(claims=claims),
        requested_model="test",
        model_version="test-version",
        input_tokens=10,
        output_tokens=20,
    )


def claim(segment_id: str = "s1", quote: str = "Czy zakaz obowiązuje jutro?") -> Claim:
    return Claim(
        id="c1",
        text="Czy wskazany zakaz obowiązuje jutro?",
        kind="verification_question",
        references=[ClaimReference(segment_id=segment_id, quote=quote)],
        ambiguities=["Nie wskazano zakazu ani daty odniesienia."],
    )


def test_extractor_preserves_provenance_and_separates_instructions() -> None:
    llm = Mock(spec=LLMAdapter)
    llm.generate_structured.return_value = extraction_result([claim()])
    segment = InputSegment(
        id="s1",
        text="Czy zakaz obowiązuje jutro?",
        origin="audio_transcript",
        start_seconds=12.5,
        end_seconds=16,
    )

    result = ClaimExtractor(llm).extract([segment])

    args = llm.generate_structured.call_args.args
    assert args[1] is ClaimExtraction
    payload = json.loads(args[0])
    assert payload["segments"][0]["start_seconds"] == 12.5
    assert payload["segments"][0]["origin"] == "audio_transcript"
    assert "untrusted data" in llm.generate_structured.call_args.kwargs["instructions"]
    assert result.data.claims[0].kind == "verification_question"
    assert result.data.claims[0].references[0].quote == segment.text
    assert result.input_tokens == 10


@pytest.mark.parametrize(
    ("bad_claim", "message"),
    [
        (claim(segment_id="missing"), "unknown input segment"),
        (claim(quote="Zmyślony cytat"), "quote absent"),
        (claim(quote=" "), "quote absent"),
    ],
)
def test_extractor_rejects_invalid_provenance(bad_claim: Claim, message: str) -> None:
    llm = Mock(spec=LLMAdapter)
    llm.generate_structured.return_value = extraction_result([bad_claim])
    with pytest.raises(RuntimeError, match=message):
        ClaimExtractor(llm).extract(
            [InputSegment(id="s1", text="Czy zakaz obowiązuje jutro?", origin="question")]
        )


def test_extractor_rejects_duplicate_ids_and_reversed_time_before_model_call() -> None:
    llm = Mock(spec=LLMAdapter)
    extractor = ClaimExtractor(llm)
    segment = InputSegment(id="s1", text="Tekst", origin="text")
    with pytest.raises(ValueError, match="unique"):
        extractor.extract([segment, segment])
    with pytest.raises(ValueError, match="end time"):
        extractor.extract(
            [InputSegment(id="s1", text="Tekst", origin="audio_transcript", start_seconds=5, end_seconds=2)]
        )
    llm.generate_structured.assert_not_called()


def test_extractor_rejects_duplicate_claim_ids() -> None:
    llm = Mock(spec=LLMAdapter)
    llm.generate_structured.return_value = extraction_result([claim(), claim()])
    with pytest.raises(RuntimeError, match="duplicate claim"):
        ClaimExtractor(llm).extract(
            [InputSegment(id="s1", text="Czy zakaz obowiązuje jutro?", origin="text")]
        )


def test_structured_generation_sends_schema_and_validates_json() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        config = payload["generationConfig"]
        assert config["responseMimeType"] == "application/json"
        assert "claims" in config["responseJsonSchema"]["properties"]
        assert config["responseJsonSchema"]["additionalProperties"] is False
        assert "responseSchema" not in config
        assert payload["systemInstruction"]["parts"][0]["text"] == "Extract claims."
        return httpx.Response(
            200,
            json={
                "modelVersion": "test-version",
                "candidates": [
                    {"content": {"parts": [{"text": '{"claims": []}'}], "role": "model"}}
                ],
                "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handle)) as http_client:
        with genai.Client(
            api_key="test-key", http_options=types.HttpOptions(httpx_client=http_client)
        ) as client:
            result = GeminiAdapter(client, "test").generate_structured(
                "input", ClaimExtraction, instructions="Extract claims."
            )
    assert isinstance(result.data, ClaimExtraction)
    assert result.data.claims == []
    assert result.model_version == "test-version"
    assert result.input_tokens == 10


@pytest.mark.parametrize("text", ['{"claims": "invalid"}', "not json", None])
def test_structured_generation_surfaces_invalid_model_output(text: str | None) -> None:
    client = Mock(spec=genai.Client)
    response = Mock(spec=types.GenerateContentResponse)
    response.text = text
    client.models.generate_content.return_value = response

    with pytest.raises(RuntimeError):
        GeminiAdapter(client, "test").generate_structured(
            "input", ClaimExtraction, instructions="Extract claims."
        )


def test_malformed_search_hints_from_the_model_are_cleaned_not_fatal() -> None:
    extracted = Claim.model_validate(
        {
            **claim().model_dump(),
            "search_queries": ["  strajk   Włochy ", "strajk Włochy", "", "x" * 200, "a", "b", "c", "d"],
            "countries": ["it", "ITA", "PL", "1X", "IT"],
        }
    )

    assert extracted.search_queries == ["strajk Włochy", "a", "b", "c"]
    assert extracted.countries == ["IT", "PL"]
