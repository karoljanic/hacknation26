import uuid
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, Mock
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient

from check_scenarios import CONCRETE_SCENARIOS, SCENARIOS, Scenario
from prawdowid.api.app import app
from prawdowid.api.check import MAX_TEXT_LENGTH, get_pipeline_factory
from prawdowid.api.rate_limit import CheckGuard, CheckLimits, get_check_guard
from prawdowid.domain.claims import InputSegment
from prawdowid.domain.enums import RunStatus
from prawdowid.domain.preparation import PreparedInput
from prawdowid.services.pipeline import PipelineResult
from prawdowid.services.preparation import MAX_MEDIA_BYTES

client = TestClient(app)

# Minimal files: correct magic bytes plus padding. The endpoint only checks
# the signature for now; the pipeline will need real media later.
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 64
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64
WEBM = b"\x1a\x45\xdf\xa3" + b"\x00" * 64

FileField = tuple[str, bytes, str]


def post_check(form: dict[str, str], file: FileField | None = None):
    files = {"file": file} if file is not None else None
    return client.post("/check", data=form, files=files)


def scenario_request(scenario: Scenario) -> tuple[dict[str, str], FileField | None]:
    form = {"type": scenario.input_type, "text": scenario.text}
    if scenario.url is not None:
        form["url"] = scenario.url
    file = ("zrzut.png", PNG, "image/png") if scenario.input_type == "image" else None
    return form, file


@pytest.fixture(autouse=True)
def generous_limits():
    """These tests send hundreds of requests from one client; rate limiting
    has its own tests in test_rate_limit.py."""
    guard = CheckGuard(CheckLimits(per_minute=10_000, per_day=10_000, global_per_day=10_000, max_concurrent=100))
    app.dependency_overrides[get_check_guard] = lambda: guard
    yield
    app.dependency_overrides.pop(get_check_guard, None)


@pytest.fixture
def stub_pipeline():
    """Deterministic, offline stand-in for the real environment-configured
    pipeline: no API keys, no network, no LLM calls. Verifies the endpoint's
    request handling and dependency wiring, not the pipeline's own logic
    (covered separately in test_pipeline.py / test_assessment.py / ...)."""
    pipeline = Mock()
    pipeline.intake.preparer.prepare_url.return_value = PreparedInput(
        segments=[InputSegment(id="article-1", text="Treść przykładowego artykułu.", origin="article")]
    )
    pipeline.intake.preparer.prepare_media.return_value = PreparedInput(
        segments=[InputSegment(id="ocr-1", text="Tekst odczytany z obrazu.", origin="ocr")]
    )
    result = PipelineResult(uuid.uuid4(), uuid.uuid4(), RunStatus.OK, claims=[])
    pipeline.run_text = AsyncMock(return_value=result)
    pipeline.run_url = AsyncMock(return_value=result)
    pipeline.run_prepared = AsyncMock(return_value=result)

    @asynccontextmanager
    async def factory():
        yield pipeline

    app.dependency_overrides[get_pipeline_factory] = lambda: factory
    yield pipeline
    app.dependency_overrides.pop(get_pipeline_factory, None)


# -- documentation ---------------------------------------------------------- #


def test_check_is_documented_in_openapi() -> None:
    spec = client.get("/openapi.json").json()
    operation = spec["paths"]["/check"]["post"]

    assert "multipart/form-data" in operation["requestBody"]["content"]
    assert {"200", "413", "415", "422", "501"} <= set(operation["responses"])
    assert {"CheckResponse", "ClaimResult", "Source", "InputType"} <= set(spec["components"]["schemas"])
    assert set(spec["components"]["schemas"]["InputType"]["enum"]) == {
        "text", "url", "question", "image", "video"
    }


# -- valid requests reach the pipeline and come back as 200 ------------------ #


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
def test_scenario_reaches_the_pipeline(scenario: Scenario, stub_pipeline) -> None:
    form, file = scenario_request(scenario)

    response = post_check(form, file)

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert (
        stub_pipeline.run_text.called or stub_pipeline.run_url.called or stub_pipeline.run_prepared.called
    )


@pytest.mark.parametrize(
    "form",
    [
        pytest.param({"type": "text", "text": "Rząd podniósł VAT do 25%."}, id="text"),
        pytest.param({"type": "question", "text": "Czy rząd podniósł VAT?"}, id="question"),
        pytest.param({"type": "url", "url": "https://example.org/artykul"}, id="url"),
        pytest.param({"type": "url", "url": "https://example.org/a", "text": "Czy to prawda?"}, id="url+question"),
        pytest.param({"type": "text", "text": "Treść", "channel": "discord"}, id="other-channel"),
        # Swagger UI always sends `url`; it is ignored unless type=url.
        pytest.param({"type": "text", "text": "Treść", "url": "string"}, id="url-ignored-for-text"),
    ],
)
def test_text_inputs_reach_the_pipeline(form: dict[str, str], stub_pipeline) -> None:
    assert post_check(form).status_code == 200


def test_text_only_goes_through_run_text(stub_pipeline) -> None:
    post_check({"type": "text", "text": "Treść."})
    stub_pipeline.run_text.assert_called_once()
    stub_pipeline.run_url.assert_not_called()


def test_url_without_question_goes_through_run_url(stub_pipeline) -> None:
    post_check({"type": "url", "url": "https://example.org/a"})
    stub_pipeline.run_url.assert_called_once()
    stub_pipeline.run_text.assert_not_called()


def test_missing_api_keys_are_a_tech_failure_not_a_verdict(monkeypatch) -> None:
    """Without stubbing: the real ``build_pipeline`` needs provider API keys."""
    for name in ("GEMINI_API_KEY", "TYPESAFE_API_KEY", "FACTCHECK_API_KEY", "EUVSDISINFO_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    response = post_check({"type": "text", "text": "Treść."})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "tech_failure"
    assert body["claims"] == []


# -- picture and video ------------------------------------------------------- #


@pytest.mark.parametrize(
    "file",
    [
        pytest.param(("zdjecie.png", PNG, "image/png"), id="png"),
        pytest.param(("zdjecie.jpg", JPEG, "image/jpeg"), id="jpeg"),
        pytest.param(("zdjecie.webp", WEBP, "image/webp"), id="webp"),
    ],
)
def test_picture_upload_is_accepted(file: FileField, stub_pipeline) -> None:
    response = post_check({"type": "image", "text": "Czy to zdjęcie jest aktualne?"}, file)

    assert response.status_code == 200
    stub_pipeline.run_prepared.assert_called_once()


@pytest.mark.parametrize(
    "file",
    [
        pytest.param(("film.mp4", MP4, "video/mp4"), id="mp4"),
        pytest.param(("film.webm", WEBM, "video/webm"), id="webm"),
        pytest.param(("film.mov", MP4, "video/quicktime"), id="mov"),
    ],
)
def test_video_upload_is_accepted(file: FileField, stub_pipeline) -> None:
    response = post_check({"type": "video"}, file)

    assert response.status_code == 200
    stub_pipeline.run_prepared.assert_called_once()


def test_picture_without_question_is_accepted(stub_pipeline) -> None:
    assert post_check({"type": "image"}, ("a.png", PNG, "image/png")).status_code == 200


@pytest.mark.parametrize(
    ("form", "file", "expected_status"),
    [
        pytest.param({"type": "image"}, None, 422, id="image-missing-file"),
        pytest.param({"type": "video"}, None, 422, id="video-missing-file"),
        pytest.param({"type": "image"}, ("film.mp4", MP4, "video/mp4"), 415, id="video-sent-as-image"),
        pytest.param({"type": "video"}, ("a.png", PNG, "image/png"), 415, id="image-sent-as-video"),
        pytest.param({"type": "image"}, ("a.gif", b"GIF89a" + b"\x00" * 16, "image/gif"), 415, id="unsupported-gif"),
        pytest.param({"type": "image"}, ("a.png", b"not really a png", "image/png"), 422, id="fake-png"),
        pytest.param({"type": "video"}, ("a.mp4", b"\x00" * 64, "video/mp4"), 422, id="fake-mp4"),
        pytest.param({"type": "image"}, ("a.png", b"", "image/png"), 422, id="empty-file"),
        pytest.param({"type": "text", "text": "Treść"}, ("a.png", PNG, "image/png"), 422, id="file-with-text"),
    ],
)
def test_invalid_media_is_rejected(
    form: dict[str, str], file: FileField | None, expected_status: int
) -> None:
    assert post_check(form, file).status_code == expected_status


def test_too_large_file_is_rejected() -> None:
    big = PNG + b"\x00" * (MAX_MEDIA_BYTES + 1 - len(PNG))

    response = post_check({"type": "image"}, ("duze.png", big, "image/png"))

    assert response.status_code == 413


# -- request validation ------------------------------------------------------ #


@pytest.mark.parametrize(
    "form",
    [
        pytest.param({}, id="missing-type"),
        pytest.param({"type": "audio", "text": "Treść"}, id="unknown-type"),
        pytest.param({"type": "text"}, id="text-missing"),
        pytest.param({"type": "question", "text": "   "}, id="blank-question"),
        pytest.param({"type": "url"}, id="url-missing"),
        pytest.param({"type": "url", "url": "nie-link"}, id="invalid-url"),
        pytest.param({"type": "text", "text": "x" * (MAX_TEXT_LENGTH + 1)}, id="text-too-long"),
        pytest.param({"type": "text", "text": "Treść", "channel": "telegram"}, id="unknown-channel"),
    ],
)
def test_invalid_request_is_rejected(form: dict[str, str]) -> None:
    assert post_check(form).status_code == 422


# -- target behaviour: expected outcomes from the scenario table ------------- #
# These fail until /check is implemented. strict=True makes them fail loudly
# once they start passing, so the marker gets removed.
# NOTE: once implemented they exercise the real pipeline (models, sources);
# run them against test doubles or move them to an integration suite.


@pytest.mark.xfail(reason="POST /check is not implemented yet", strict=True)
@pytest.mark.parametrize("scenario", CONCRETE_SCENARIOS, ids=lambda s: s.id)
def test_scenario_expected_outcome(scenario: Scenario) -> None:
    form, file = scenario_request(scenario)

    response = post_check(form, file)

    assert response.status_code == 200
    result: dict[str, Any] = response.json()
    assert result["status"] == "ok"
    assert result["claims"], "expected at least one extracted claim"

    verdicts = {claim["verdict"] for claim in result["claims"]}
    assert verdicts & scenario.expected_verdicts, scenario.notes

    if scenario.source_domain is not None:
        domains = {
            urlparse(source["url"]).hostname or ""
            for claim in result["claims"]
            for source in claim["sources"]
        }
        assert any(d == scenario.source_domain or d.endswith("." + scenario.source_domain) for d in domains)

    if "abstain" in scenario.tags:
        # No evidence: every claim abstains instead of guessing a verdict.
        assert all(claim["verdict"] == "insufficient_evidence" for claim in result["claims"])
