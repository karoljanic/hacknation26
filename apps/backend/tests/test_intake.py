"""Intake against a real Postgres (skipped when DATABASE_URL is unreachable).

Start it with `dev-services up -d`; migrations are applied automatically.
"""

import math
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, select, text
from sqlalchemy.exc import OperationalError

from prawdowid.ai.adapters.embedding import EmbeddingResult
from prawdowid.ai.adapters.llm import StructuredResult
from prawdowid.domain.claims import Claim as ExtractedClaim
from prawdowid.domain.claims import ClaimExtraction, ClaimReference
from prawdowid.domain.enums import Channel, ClaimKind, RunStatus, SubmissionClaimRelation
from prawdowid.persistence.db import get_engine, session_factory
from prawdowid.persistence.models import EMBEDDING_DIM, Claim, Submission, SubmissionClaim, VerificationRun
from prawdowid.services.claims import ClaimExtractor
from prawdowid.services.intake import ClaimIntake
from prawdowid.services.preparation import InputPreparer

BACKEND = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def database() -> None:
    try:
        with get_engine().connect() as connection:
            connection.execute(text("SELECT 1"))
    except OperationalError:
        pytest.skip("PostgreSQL not reachable (run `dev-services up -d`)")
    command.upgrade(Config(str(BACKEND / "alembic.ini")), "head")


class FakeLLM:
    """Returns claims whose quotes are copied from the prompt's first segment."""

    def __init__(self, items: list[tuple[str, str]], *, fail: Exception | None = None) -> None:
        self.items = items  # (text, kind)
        self.fail = fail
        self.prompts: list[str] = []

    def generate_structured(self, prompt: str, schema: type, *, instructions: str, media: Any = None):
        self.prompts.append(prompt)
        if self.fail is not None:
            raise self.fail
        import json

        segment = json.loads(prompt)["segments"][0]
        claims = [
            ExtractedClaim(
                id=f"c{i}",
                text=claim_text,
                kind=kind,
                references=[ClaimReference(segment_id=segment["id"], quote=segment["text"][:10])],
                ambiguities=[],
            )
            for i, (claim_text, kind) in enumerate(self.items)
        ]
        return StructuredResult(ClaimExtraction(claims=claims), "fake-model", "v1", 10, 5)

    def generate(self, prompt: str, *, search: bool = False):  # pragma: no cover
        raise NotImplementedError


class FakeEmbedder:
    """Same leading word -> nearly identical vector; different word -> orthogonal."""

    def embed(self, content: str) -> EmbeddingResult:
        axis = sum(map(ord, content.split()[0].casefold())) % EMBEDDING_DIM
        vector = [0.0] * EMBEDDING_DIM
        vector[axis] = 1.0
        vector[(axis + 1) % EMBEDDING_DIM] = 0.05 * (len(content) % 3)
        norm = math.sqrt(sum(v * v for v in vector))
        return EmbeddingResult([v / norm for v in vector], "fake-embedding")


class NoFetch:
    def fetch(self, url: str):  # pragma: no cover
        raise AssertionError("not used")


def make_intake(llm: FakeLLM, embedder: FakeEmbedder | None = None) -> ClaimIntake:
    return ClaimIntake(
        session_factory=session_factory(),
        preparer=InputPreparer(llm=llm, fetcher=NoFetch()),
        extractor=ClaimExtractor(llm),
        embedder=embedder,
    )


@pytest.fixture
def cleanup(database: None) -> Iterator[list[uuid.UUID]]:
    submission_ids: list[uuid.UUID] = []
    yield submission_ids
    with session_factory()() as session, session.begin():
        claim_ids = session.scalars(
            select(SubmissionClaim.claim_id).where(SubmissionClaim.submission_id.in_(submission_ids))
        ).all()
        session.execute(delete(Submission).where(Submission.id.in_(submission_ids)))
        session.execute(delete(Claim).where(Claim.id.in_(claim_ids)))


def test_stores_redacted_submission_claims_and_run(cleanup: list[uuid.UUID]) -> None:
    tag = uuid.uuid4().hex[:8]
    llm = FakeLLM([(f"Rząd {tag} podniósł VAT do 25%.", "factual_claim"),
                   (f"Czy {tag} VAT wzrośnie?", "verification_question")])
    result = make_intake(llm).submit_text(
        f"Kolega (tel. 600123456) mówi, że rząd {tag} podniósł VAT.", channel=Channel.WEB
    )
    cleanup.append(result.submission_id)

    assert result.status is RunStatus.OK
    assert [c.match for c in result.claims] == ["new", "new"]
    assert [c.relation for c in result.claims] == [
        SubmissionClaimRelation.STATES,
        SubmissionClaimRelation.ASKS,
    ]
    assert "600123456" not in llm.prompts[0]  # PII never reaches the model
    with session_factory()() as session:
        submission = session.get_one(Submission, result.submission_id)
        assert submission.redacted_text is not None and "[TELEFON]" in submission.redacted_text
        run = session.get_one(VerificationRun, result.run_id)
        assert run.status is RunStatus.OK and run.finished_at is not None
        assert run.models[0]["role"] == "extraction"
        kinds = {c.kind for c in session.scalars(select(Claim).where(Claim.id.in_([c.claim_id for c in result.claims])))}
        assert kinds == {ClaimKind.FACTUAL_CLAIM, ClaimKind.VERIFICATION_QUESTION}


def test_repeated_claims_are_linked_not_duplicated(cleanup: list[uuid.UUID]) -> None:
    tag = uuid.uuid4().hex[:8]
    embedder = FakeEmbedder()
    first = make_intake(FakeLLM([(f"{tag} Inflacja wyniosła 3,6%.", "factual_claim")]), embedder)
    result1 = first.submit_text(f"{tag} inflacja", channel=Channel.WEB)
    # Exact match modulo case/punctuation.
    result2 = make_intake(FakeLLM([(f"{tag} inflacja wyniosła 3,6%", "factual_claim")]), embedder).submit_text(
        f"{tag} inflacja?", channel=Channel.DISCORD
    )
    # Different wording, near-identical embedding.
    result3 = make_intake(FakeLLM([(f"{tag} Inflacja to 3,6 procent", "factual_claim")]), embedder).submit_text(
        f"{tag} ile inflacja", channel=Channel.DEMO
    )
    cleanup.extend([result1.submission_id, result2.submission_id, result3.submission_id])

    claim_id = result1.claims[0].claim_id
    assert result2.claims[0].claim_id == claim_id and result2.claims[0].match == "exact"
    assert result3.claims[0].claim_id == claim_id and result3.claims[0].match.startswith("similar:")


def test_provider_failure_is_a_run_status_not_a_verdict(cleanup: list[uuid.UUID]) -> None:
    llm = FakeLLM([], fail=RuntimeError("429 quota exceeded"))
    result = make_intake(llm).submit_text("Coś do sprawdzenia.", channel=Channel.WEB)
    cleanup.append(result.submission_id)

    assert result.status is RunStatus.TECH_FAILURE
    assert result.claims == [] and result.message
    with session_factory()() as session:
        run = session.get_one(VerificationRun, result.run_id)
        assert run.status is RunStatus.TECH_FAILURE and "429" in (run.error or "")


def test_no_claims_asks_for_clarification(cleanup: list[uuid.UUID]) -> None:
    result = make_intake(FakeLLM([])).submit_text("Dzień dobry!", channel=Channel.WEB)
    cleanup.append(result.submission_id)
    assert result.status is RunStatus.NEEDS_CLARIFICATION


def test_similar_claims_differing_in_a_number_are_not_merged(cleanup: list[uuid.UUID]) -> None:
    tag = uuid.uuid4().hex[:8]
    embedder = FakeEmbedder()  # same leading word -> nearly identical vectors
    first = make_intake(FakeLLM([(f"{tag} VAT wynosi 23%.", "factual_claim")]), embedder).submit_text(
        "x", channel=Channel.WEB
    )
    second = make_intake(FakeLLM([(f"{tag} VAT wynosi 25%.", "factual_claim")]), embedder).submit_text(
        "x", channel=Channel.WEB
    )
    cleanup.extend([first.submission_id, second.submission_id])

    assert [c.match for c in second.claims] == ["new"]
    assert second.claims[0].claim_id != first.claims[0].claim_id


def test_embedding_failure_does_not_fail_the_submission(cleanup: list[uuid.UUID]) -> None:
    class BrokenEmbedder:
        def embed(self, content: str) -> EmbeddingResult:
            raise RuntimeError("quota exceeded")

    tag = uuid.uuid4().hex[:8]
    result = make_intake(
        FakeLLM([(f"{tag} Inflacja wyniosła 3,6%.", "factual_claim")]), BrokenEmbedder()
    ).submit_text("x", channel=Channel.WEB)
    cleanup.append(result.submission_id)

    assert result.status is RunStatus.OK
    assert [c.match for c in result.claims] == ["new"]
    with session_factory()() as session:
        models = session.get_one(VerificationRun, result.run_id).models
        assert {"role": "embedding", "error": "RuntimeError", "fallback": "exact_text"} in models
