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


def test_verify_endpoint_requires_image_file_and_rejects_text_only() -> None:
    resp = client.post("/verify", data={"text": "Potwierdzone " + ("a" * 32)})
    assert resp.status_code == 422
