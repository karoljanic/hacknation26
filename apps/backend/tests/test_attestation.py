from contextlib import asynccontextmanager
from datetime import datetime, timezone
import uuid
from unittest.mock import AsyncMock, Mock

from fastapi.testclient import TestClient
import pytest

from prawdowid.ai.adapters.llm import StructuredResult
from prawdowid.api.app import app
from prawdowid.api.check import get_pipeline_factory
from prawdowid.api.rate_limit import CheckGuard, CheckLimits, get_check_guard
from prawdowid.api.verify import get_verifier_factory
from prawdowid.domain.enums import RunStatus, Strength, Verdict
from prawdowid.domain.evidence import (
    AssessmentReasons,
    ClaimAssessmentOutput,
    EvidenceForAssessment,
    EvidenceItem,
)
from prawdowid.security.attestation import (
    HASH_HEX_LENGTH,
    SIGNING_KEY_ENV,
    SignableClaim,
    clean_hash,
    compute_answer_hash,
    format_answer_text,
    normalize_for_hash,
    verify_answer_hash,
)
from prawdowid.services.attestation import (
    AnswerVerifier,
    ExtractedClaim,
    ScreenshotExtraction,
)
from prawdowid.services.pipeline import ClaimOutcome, PipelineResult

client = TestClient(app)
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
SECRET = "test-signing-key-prawdowid-2026"


@pytest.fixture(autouse=True)
def generous_limits():
    guard = CheckGuard(
        CheckLimits(per_minute=10_000, per_day=10_000, global_per_day=10_000, max_concurrent=100)
    )
    app.dependency_overrides[get_check_guard] = lambda: guard
    yield
    app.dependency_overrides.pop(get_check_guard, None)


def test_normalize_for_hash_replaces_polish_chars_whitespace_and_punctuation() -> None:
    assert normalize_for_hash("Żółć,   gęślą — jaźń!") == "zolc;gesla;jazn"
    assert (
        normalize_for_hash("  Łódź: 100%, 2026-11-21\n\tPotwierdzone... ")
        == "lodz;100;2026;11;21;potwierdzone"
    )
    assert normalize_for_hash("Woda wrze w 100°C.") == normalize_for_hash("Woda   wrze\nw 100 C")
    assert normalize_for_hash("   --- !!!   ") == ""


def test_hmac_128bit_excludes_inline_hash_and_survives_ocr_noise() -> None:
    signed_text = format_answer_text(
        [
            SignableClaim(
                text="We Włoszech odbył się strajk generalny.",
                verdict=Verdict.CONFIRMED,
                strength=Strength.STRONG,
                explanation="Związki zawodowe przeprowadziły ogólnokrajowy strajk.",
            )
        ]
    )
    digest = compute_answer_hash(signed_text, secret_key=SECRET)
    assert len(digest) == HASH_HEX_LENGTH == 32
    assert clean_hash("a" * 64) is None

    with_inline_hash = (
        "We Wloszech odbyl sie strajk generalny\n"
        f"Potwierdzone {digest} Silna podstawa\n"
        "Zwiazki zawodowe przeprowadzily ogolnokrajowy strajk"
    )
    assert compute_answer_hash(with_inline_hash, secret_key=SECRET) == digest
    assert verify_answer_hash(with_inline_hash, digest, secret_key=SECRET) is True
    assert (
        verify_answer_hash(with_inline_hash.replace("Potwierdzone", "Falszywe"), digest, secret_key=SECRET)
        is False
    )


def test_check_returns_128bit_answer_hash(monkeypatch) -> None:
    monkeypatch.setenv(SIGNING_KEY_ENV, SECRET)

    item = EvidenceItem(
        source="google_fact_check",
        kind="claim_review",
        title="Komunikat",
        url="https://www.gov.pl/web/mswia/komunikat",
        publisher="MSWiA",
        rating="Prawda",
        snippet="Strajk odbył się w całym kraju.",
        published_at=datetime(2026, 11, 20, tzinfo=timezone.utc),
    )
    assessment = ClaimAssessmentOutput(
        claim_id="c1",
        verdict="confirmed",
        strength="strong",
        reasons=AssessmentReasons(source_tiers=[1], independent_sources=1, conflicting_sources=False),
        explanation="Związki zawodowe przeprowadziły ogólnokrajowy strajk.",
        citations=[{"evidence_index": 0, "quote": "Strajk odbył się w całym kraju.", "relation": "supports"}],
    )
    outcome = ClaimOutcome(
        "c1",
        "We Włoszech odbył się strajk generalny.",
        "factual_claim",
        assessment,
        "model:test",
        [EvidenceForAssessment(item=item, text=item.snippet, fully_fetched=False)],
    )
    pipeline = Mock()
    pipeline.run_text = AsyncMock(
        return_value=PipelineResult(uuid.uuid4(), uuid.uuid4(), RunStatus.OK, claims=[outcome])
    )

    @asynccontextmanager
    async def factory():
        yield pipeline

    app.dependency_overrides[get_pipeline_factory] = lambda: factory
    try:
        check_resp = client.post(
            "/check", data={"type": "text", "text": "We Włoszech odbył się strajk generalny."}
        )
    finally:
        app.dependency_overrides.pop(get_pipeline_factory, None)

    assert check_resp.status_code == 200
    body = check_resp.json()
    assert body["answer_hash"] is not None
    assert len(body["answer_hash"]) == 32


def test_verify_endpoint_requires_image_file_and_rejects_text_only() -> None:
    resp = client.post("/verify", data={"text": "Potwierdzone " + ("a" * 32)})
    assert resp.status_code == 422


def test_verify_endpoint_accepts_authentic_screenshot_and_rejects_tampered() -> None:
    signed_text = format_answer_text(
        [
            SignableClaim(
                text="Woda wrze w 100°C.",
                verdict=Verdict.CONFIRMED,
                strength=Strength.STRONG,
                explanation="Przy ciśnieniu normalnym temperatura wrzenia wynosi 100°C.",
            )
        ]
    )
    digest = compute_answer_hash(signed_text, secret_key=SECRET)

    def make_factory(extraction: ScreenshotExtraction):
        class StubLLM:
            def generate_structured(self, prompt, schema, *, instructions, media=None):
                return StructuredResult(
                    data=extraction,
                    requested_model="stub",
                    model_version="stub-1",
                    input_tokens=10,
                    output_tokens=10,
                )

        @asynccontextmanager
        async def verifier_factory():
            yield AnswerVerifier(secret_key=SECRET, llm=StubLLM())

        return verifier_factory

    authentic_extraction = ScreenshotExtraction(
        status="extracted",
        hash_value=f"{digest[:16]}\n{digest[16:]}",
        claims=[
            ExtractedClaim(
                text="Woda wrze w 100 C",
                verdict="Potwierdzone",
                strength="Silna podstawa",
                explanation="Przy cisnieniu normalnym temperatura wrzenia wynosi 100 C",
            )
        ],
    )
    app.dependency_overrides[get_verifier_factory] = lambda: make_factory(authentic_extraction)
    try:
        resp_ok = client.post("/verify", files={"file": ("zrzut.png", PNG, "image/png")})
    finally:
        app.dependency_overrides.pop(get_verifier_factory, None)

    assert resp_ok.status_code == 200
    assert resp_ok.json()["authentic"] is True
    assert resp_ok.json()["status"] == "verified"
    assert resp_ok.json()["extracted_hash"] == digest

    tampered_extraction = authentic_extraction.model_copy(
        update={"claims": [authentic_extraction.claims[0].model_copy(update={"verdict": "Fałszywe"})]}
    )
    app.dependency_overrides[get_verifier_factory] = lambda: make_factory(tampered_extraction)
    try:
        resp_bad = client.post("/verify", files={"file": ("zrzut.png", PNG, "image/png")})
    finally:
        app.dependency_overrides.pop(get_verifier_factory, None)

    assert resp_bad.status_code == 200
    assert resp_bad.json()["authentic"] is False
    assert resp_bad.json()["status"] == "invalid_hash"