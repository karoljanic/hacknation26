from unittest.mock import Mock

import pytest
from google import genai
from google.genai import types
from typesafe_sdk import SystemOneResponse, TypeSafeClient

from prawdowid.ai.adapters.decision import (
    BooleanQuestion,
    BoxQuestion,
    ChoiceQuestion,
    ScoreQuestion,
)
from prawdowid.ai.adapters.media import Media
from prawdowid.ai.instances.gemini import GeminiAdapter
from prawdowid.ai.instances.gemini_embeddings import GeminiEmbeddingAdapter
from prawdowid.ai.instances.jev import JevAdapter


def gemini_response(grounded: bool = False) -> types.GenerateContentResponse:
    candidate: dict = {"content": {"parts": [{"text": "Odpowiedź"}]}}
    if grounded:
        candidate["grounding_metadata"] = {
            "web_search_queries": ["zapytanie"],
            "grounding_chunks": [{"web": {"uri": "https://example.org", "title": "Źródło"}}],
            "grounding_supports": [
                {"segment": {"text": "Odpowiedź", "start_index": 0, "end_index": 9}, "grounding_chunk_indices": [0]}
            ],
        }
    return types.GenerateContentResponse.model_validate(
        {
            "model_version": "test-version",
            "candidates": [candidate],
            "usage_metadata": {"prompt_token_count": 12, "candidates_token_count": 3},
        }
    )


def test_gemini_search_preserves_grounding_and_usage() -> None:
    client = Mock(spec=genai.Client)
    client.models.generate_content.return_value = gemini_response(grounded=True)

    result = GeminiAdapter(client, "search-model").generate("Sprawdź", search=True)

    kwargs = client.models.generate_content.call_args.kwargs
    assert kwargs["model"] == "search-model"
    assert kwargs["config"].tools[0].google_search is not None
    assert result.text == "Odpowiedź"
    assert result.model_version == "test-version"
    assert result.input_tokens == 12
    assert result.output_tokens == 3
    assert result.grounding is not None
    assert result.grounding["web_search_queries"] == ["zapytanie"]
    assert result.grounding["grounding_supports"][0]["grounding_chunk_indices"] == [0]


def test_gemini_does_not_invent_grounding_when_search_not_used() -> None:
    client = Mock(spec=genai.Client)
    client.models.generate_content.return_value = gemini_response()
    adapter = GeminiAdapter(client, "fast-model")

    result = adapter.generate("Test")

    assert client.models.generate_content.call_args.kwargs["config"].tools is None
    assert result.grounding is None
    assert adapter.generate("Test", search=True).grounding is None


def test_gemini_rejects_empty_input_and_empty_output() -> None:
    client = Mock(spec=genai.Client)
    adapter = GeminiAdapter(client, "fast-model")
    with pytest.raises(ValueError, match="Prompt"):
        adapter.generate(" ")
    client.models.generate_content.return_value = types.GenerateContentResponse()
    with pytest.raises(RuntimeError, match="no text"):
        adapter.generate("Test")


def decision_response() -> SystemOneResponse:
    return SystemOneResponse.model_validate(
        {
            "model": "jev-test",
            "answers": {
                "checkable": {"type": "noul", "noul": 0.9},
                "topic": {
                    "type": "choice",
                    "choice": "law",
                    "confidence": 0.8,
                    "probabilities": {"law": 0.9, "other": 0.1},
                },
                "clarity": {
                    "type": "score",
                    "score": 0.8,
                    "confidence": 0.8,
                    "legend": {0: "unclear", 1: "clear"},
                    "probabilities": {0: 0.2, 1: 0.8},
                },
            },
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
    )


def test_jev_forwards_multiple_typed_questions_and_response() -> None:
    client = Mock(spec=TypeSafeClient)
    client.system_one.return_value = decision_response()
    questions = {
        "checkable": BooleanQuestion(instructions="Can this be checked?"),
        "topic": ChoiceQuestion(instructions="Topic?", criteria={"law": "Law", "other": "Other"}),
        "clarity": ScoreQuestion(instructions="Clarity?", criteria=["unclear", "clear"]),
    }

    result = JevAdapter(client, "jev-test").decide("A claim", questions)

    kwargs = client.system_one.call_args.kwargs
    assert kwargs["state"] == "A claim"
    assert kwargs["model"] == "jev-test"
    assert kwargs["extra_headers"] is None
    assert {name: question.type for name, question in kwargs["questions"].items()} == {
        "checkable": "noul", "topic": "choice", "clarity": "score"
    }
    assert result.answers["checkable"].model_dump()["noul"] == 0.9
    assert result.answers["topic"].model_dump()["choice"] == "law"
    assert result.answers["clarity"].model_dump()["score"] == 0.8


def test_jev_box_requires_explicit_opt_in_and_sets_header() -> None:
    client = Mock(spec=TypeSafeClient)
    client.system_one.return_value = SystemOneResponse.model_validate(
        {
            "model": "jev-test",
            "answers": {
                "box": {
                    "type": "box",
                    "box": {"top": 0.1, "bottom": 0.8, "left": 0.2, "right": 0.9},
                    "confidence": None,
                }
            },
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
    )
    adapter = JevAdapter(client)
    questions = {"box": BoxQuestion(instructions="Find the text")}
    with pytest.raises(ValueError, match="require an image"):
        adapter.decide("image", questions)

    response = adapter.decide("image", questions, image=Media(b"image", "image/png"))

    assert client.system_one.call_args.kwargs["extra_headers"] == {
        "TypeSafe-Danger-Zone-Images": "true"
    }
    assert response.answers["box"].model_dump()["box"]["left"] == 0.2


def test_jev_rejects_missing_or_wrong_answer_types() -> None:
    client = Mock(spec=TypeSafeClient)
    client.system_one.return_value = decision_response()
    adapter = JevAdapter(client)
    with pytest.raises(ValueError, match="At least one"):
        adapter.decide("state", {})
    with pytest.raises(RuntimeError, match="does not match"):
        adapter.decide("state", {"missing": BooleanQuestion(instructions="Question?")})
    questions = {
        "checkable": ChoiceQuestion(instructions="Question?", criteria={"yes": None, "no": None}),
        "topic": ChoiceQuestion(instructions="Question?", criteria={"yes": None, "no": None}),
        "clarity": ScoreQuestion(instructions="Question?", criteria=["low", "high"]),
    }
    with pytest.raises(RuntimeError, match="unexpected answer type"):
        adapter.decide("state", questions)


def test_provider_errors_are_not_reported_as_success() -> None:
    client = Mock(spec=TypeSafeClient)
    client.system_one.side_effect = RuntimeError("provider unavailable")
    with pytest.raises(RuntimeError, match="provider unavailable"):
        JevAdapter(client).decide("state", {"q": BooleanQuestion(instructions="Question?")})

    gemini_client = Mock(spec=genai.Client)
    gemini_client.models.generate_content.side_effect = RuntimeError("provider unavailable")
    with pytest.raises(RuntimeError, match="provider unavailable"):
        GeminiAdapter(gemini_client, "test-model").generate("prompt")


def test_embeddings_reject_empty_input_and_invalid_provider_response() -> None:
    client = Mock(spec=genai.Client)
    adapter = GeminiEmbeddingAdapter(client)
    with pytest.raises(ValueError, match="Text"):
        adapter.embed(" ")
    with pytest.raises(ValueError, match="Media"):
        adapter.embed(Media(b"", "image/png"))
    client.models.embed_content.return_value = types.EmbedContentResponse(embeddings=[])
    with pytest.raises(RuntimeError, match="exactly one"):
        adapter.embed("text")
    client.models.embed_content.return_value = types.EmbedContentResponse(
        embeddings=[types.ContentEmbedding(values=[float("nan")])]
    )
    with pytest.raises(RuntimeError, match="invalid vector"):
        adapter.embed("text")
