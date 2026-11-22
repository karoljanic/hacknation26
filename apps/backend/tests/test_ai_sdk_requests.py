import base64
import json

import httpx
import pytest
import httpx2
from google import genai
from google.genai import types
from typesafe_sdk import RetryPolicy, TypeSafeClient

from prawdowid.ai.adapters.decision import (
    BooleanQuestion,
    BoxQuestion,
    ChoiceQuestion,
    ScoreQuestion,
)
from prawdowid.ai.adapters.media import Media
from prawdowid.ai.adapters.llm import ContentBlocked
from prawdowid.ai.instances.gemini import GeminiAdapter
from prawdowid.ai.instances.gemini_embeddings import GeminiEmbeddingAdapter
from prawdowid.ai.instances.jev import JevAdapter


def test_gemini_sdk_sends_search_tool_and_returns_citations() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["tools"] == [{"googleSearch": {}}]
        assert body["contents"][0]["parts"][0]["text"] == "Find evidence"
        return httpx.Response(
            200,
            json={
                "modelVersion": "test-version",
                "candidates": [
                    {
                        "content": {"parts": [{"text": "Result"}], "role": "model"},
                        "groundingMetadata": {
                            "webSearchQueries": ["evidence"],
                            "groundingChunks": [
                                {"web": {"uri": "https://example.org", "title": "Source"}}
                            ],
                            "groundingSupports": [
                                {
                                    "segment": {"text": "Result", "startIndex": 0, "endIndex": 6},
                                    "groundingChunkIndices": [0],
                                }
                            ],
                        },
                    }
                ],
            },
        )

    transport = httpx.MockTransport(handle)
    with httpx.Client(transport=transport) as http_client:
        with genai.Client(
            api_key="test-key",
            http_options=types.HttpOptions(httpx_client=http_client),
        ) as client:
            result = GeminiAdapter(client, "test-model").generate("Find evidence", search=True)

    assert result.text == "Result"
    assert result.grounding is not None
    assert result.grounding["web_search_queries"] == ["evidence"]
    assert result.grounding["grounding_chunks"][0]["web"]["uri"] == "https://example.org"


def test_jev_sdk_sends_typed_questions_and_parses_api_response() -> None:
    def handle(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        assert request.url.path == "/v1/systemone"
        assert body["model"] == "jev-latest"
        assert body["state"] == "A claim"
        assert body["questions"]["checkable"]["type"] == "noul"
        assert body["questions"]["topic"]["type"] == "choice"
        assert body["questions"]["clarity"]["type"] == "score"
        return httpx2.Response(
            200,
            json={
                "model": "jev-test",
                "answers": {
                    "checkable": {"type": "noul", "noul": 0.8},
                    "topic": {
                        "type": "choice",
                        "choice": "law",
                        "confidence": 0.7,
                        "probabilities": {"law": 0.8, "other": 0.2},
                    },
                    "clarity": {
                        "type": "score",
                        "score": 0.7,
                        "confidence": 0.4,
                        "legend": {"0": "unclear", "1": "clear"},
                        "probabilities": {"0": 0.3, "1": 0.7},
                    },
                },
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        )

    with TypeSafeClient(
        api_key="test-key",
        transport=httpx2.MockTransport(handle),
        retry=RetryPolicy(max_retries=0),
    ) as client:
        result = JevAdapter(client).decide(
            "A claim",
            {
                "checkable": BooleanQuestion(instructions="Checkable?"),
                "topic": ChoiceQuestion(instructions="Topic?", criteria={"law": None, "other": None}),
                "clarity": ScoreQuestion(instructions="Clarity?", criteria=["unclear", "clear"]),
            },
        )
    assert result.answers["checkable"].model_dump()["noul"] == 0.8
    assert result.answers["clarity"].model_dump()["legend"][0] == "unclear"


def test_jev_sdk_sends_image_with_explicit_opt_in() -> None:
    def handle(request: httpx2.Request) -> httpx2.Response:
        assert request.headers["TypeSafe-Danger-Zone-Images"] == "true"
        assert request.headers["Content-Type"].startswith("multipart/form-data;")
        return httpx2.Response(
            200,
            json={
                "model": "jev-test",
                "answers": {
                    "region": {
                        "type": "box",
                        "box": {"top": 0.1, "bottom": 0.8, "left": 0.2, "right": 0.9},
                        "confidence": None,
                    }
                },
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        )

    image = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
        "/x8AAwMCAO+/lckAAAAASUVORK5CYII="
    )
    with TypeSafeClient(
        api_key="test-key",
        transport=httpx2.MockTransport(handle),
        retry=RetryPolicy(max_retries=0),
    ) as client:
        result = JevAdapter(client).decide(
            "Inspect the image.",
            {"region": BoxQuestion(instructions="Find the text in `image`.")},
            image=Media(image, "image/png"),
        )
    assert result.answers["region"].model_dump()["box"]["left"] == 0.2


def test_gemini_embedding_serializes_text_and_image() -> None:
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"embeddings": [{"values": [0.25, 0.75]}]})

    with httpx.Client(transport=httpx.MockTransport(handle)) as http_client:
        with genai.Client(api_key="test-key", http_options=types.HttpOptions(httpx_client=http_client)) as client:
            adapter = GeminiEmbeddingAdapter(client)
            assert adapter.embed("tekst").values == [0.25, 0.75]
            assert adapter.embed(Media(b"image-bytes", "image/png")).values == [0.25, 0.75]

    assert requests[0]["requests"][0]["content"]["parts"][0]["text"] == "tekst"
    assert requests[1]["requests"][0]["content"]["parts"][0]["inline_data"] == {
        "mime_type": "image/png",
        "data": base64.b64encode(b"image-bytes").decode(),
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"promptFeedback": {"blockReason": "SAFETY"}},
        {"candidates": [{"finishReason": "PROHIBITED_CONTENT"}]},
    ],
)
def test_gemini_refusal_is_reported_as_content_blocked_not_an_outage(payload: dict) -> None:
    sent: dict = {}

    def handle(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(request.content))
        return httpx.Response(200, json=payload)

    with httpx.Client(transport=httpx.MockTransport(handle)) as http_client:
        with genai.Client(api_key="test-key", http_options=types.HttpOptions(httpx_client=http_client)) as client:
            with pytest.raises(ContentBlocked):
                GeminiAdapter(client, "test-model").generate("Czy nożownik zabił trzy osoby?")

    thresholds = {setting["threshold"] for setting in sent["safetySettings"]}
    assert thresholds == {"BLOCK_ONLY_HIGH"}
