import asyncio
import uuid
from unittest.mock import AsyncMock, Mock

import pytest

from prawdowid.domain.enums import Channel, ClaimKind, RunStatus, SubmissionClaimRelation
from prawdowid.domain.evidence import (
    AssessmentReasons,
    ClaimAssessmentOutput,
    ClaimEvidence,
    EvidenceForAssessment,
    EvidenceItem,
    EvidenceSearchResult,
    SubmissionAssessmentOutput,
)
from prawdowid.persistence.models import VerificationRun
from prawdowid.services.intake import IntakeResult, StoredClaim
from prawdowid.services.pipeline import VerificationPipeline


class FakeSession:
    def __init__(self, runs: dict[uuid.UUID, VerificationRun]) -> None:
        self.runs = runs

    def __enter__(self) -> "FakeSession":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def begin(self) -> "FakeSession":
        return self

    def get_one(self, _model: type, run_id: uuid.UUID) -> VerificationRun:
        return self.runs[run_id]


def stored(claim_id: uuid.UUID, text: str, kind: ClaimKind = ClaimKind.FACTUAL_CLAIM) -> StoredClaim:
    return StoredClaim(claim_id, text, text, kind, SubmissionClaimRelation.STATES, "new")


def intake_result(status: RunStatus, claims: list[StoredClaim], *, run_id: uuid.UUID, message=None) -> IntakeResult:
    return IntakeResult(uuid.uuid4(), run_id, status, claims, message)


def evidence_item(url: str = "https://demagog.org.pl/a") -> EvidenceItem:
    return EvidenceItem(
        source="google_fact_check", kind="claim_review", title="Analiza", url=url,
        publisher="Demagog", rating="Fałsz", snippet="Demagog ocenił tezę jako fałsz.",
    )


def make_pipeline(*, run: VerificationRun, intake=None, evidence_search=None, sublinks=None, assessment=None):
    fake_intake = intake or Mock()
    fake_intake.session_factory = lambda: FakeSession({run.id: run})
    return VerificationPipeline(
        intake=fake_intake,
        evidence_search=evidence_search or Mock(),
        sublinks=sublinks or Mock(),
        assessment=assessment or Mock(),
    )


@pytest.fixture(autouse=True)
def stub_record_outcome(monkeypatch):
    """This file tests orchestration only; DB persistence has its own focused
    tests in test_persistence_evidence.py against a real Postgres."""
    monkeypatch.setattr("prawdowid.persistence.evidence.record_outcome", Mock())


def test_non_ok_intake_status_short_circuits_without_further_calls() -> None:
    run_id = uuid.uuid4()
    intake = Mock()
    intake.submit_text = Mock(
        return_value=intake_result(RunStatus.NEEDS_CLARIFICATION, [], run_id=run_id, message="Doprecyzuj pytanie.")
    )
    evidence_search = Mock()
    pipeline = make_pipeline(
        run=VerificationRun(id=run_id, status=RunStatus.RUNNING, pipeline_version="t"),
        intake=intake, evidence_search=evidence_search,
    )

    result = asyncio.run(pipeline.run_text("???", channel=Channel.WEB))

    assert result.status == RunStatus.NEEDS_CLARIFICATION
    assert result.claims == []
    evidence_search.search.assert_not_called()


def test_opinion_claim_is_deterministic_and_skips_evidence() -> None:
    run_id = uuid.uuid4()
    claim_id = uuid.uuid4()
    intake = Mock()
    intake.submit_text.return_value = intake_result(
        RunStatus.OK, [stored(claim_id, "To moim zdaniem źle.", ClaimKind.OPINION)], run_id=run_id
    )
    evidence_search = Mock()
    evidence_search.search = AsyncMock(
        return_value=EvidenceSearchResult(claims=[], skipped_opinion_ids=[str(claim_id)])
    )
    assessment = Mock()
    pipeline = make_pipeline(
        run=VerificationRun(id=run_id, status=RunStatus.RUNNING, pipeline_version="t"),
        intake=intake, evidence_search=evidence_search, assessment=assessment,
    )

    result = asyncio.run(pipeline.run_text("To moim zdaniem źle.", channel=Channel.WEB))

    assert result.status == RunStatus.OK
    assert len(result.claims) == 1
    assert result.claims[0].assessment.verdict == "unverifiable"
    assessment.assess.assert_not_called()


def test_claim_without_any_evidence_abstains_without_calling_the_llm() -> None:
    run_id, claim_id = uuid.uuid4(), uuid.uuid4()
    intake = Mock()
    intake.submit_text.return_value = intake_result(
        RunStatus.OK, [stored(claim_id, "Nowa teza bez dowodów.")], run_id=run_id
    )
    evidence_search = Mock()
    evidence_search.search = AsyncMock(
        return_value=EvidenceSearchResult(
            claims=[ClaimEvidence(claim_id=str(claim_id), items=[], unavailable_sources=["euvsdisinfo"])],
            skipped_opinion_ids=[],
        )
    )
    assessment = Mock()
    pipeline = make_pipeline(
        run=VerificationRun(id=run_id, status=RunStatus.RUNNING, pipeline_version="t"),
        intake=intake, evidence_search=evidence_search, assessment=assessment,
    )

    result = asyncio.run(pipeline.run_text("Nowa teza bez dowodów.", channel=Channel.WEB))

    assert result.claims[0].assessment.verdict == "insufficient_evidence"
    assert "euvsdisinfo" in result.claims[0].assessment.limitations
    assessment.assess.assert_not_called()


def test_claim_with_evidence_goes_through_sublinks_and_assessment() -> None:
    run_id, claim_id = uuid.uuid4(), uuid.uuid4()
    intake = Mock()
    intake.submit_url.return_value = intake_result(
        RunStatus.OK, [stored(claim_id, "Rząd podniósł VAT.")], run_id=run_id
    )
    item = evidence_item()
    evidence_search = Mock()
    evidence_search.search = AsyncMock(
        return_value=EvidenceSearchResult(
            claims=[ClaimEvidence(claim_id=str(claim_id), items=[item])], skipped_opinion_ids=[],
        )
    )
    sublinks = Mock()
    sublinks.select.return_value = [EvidenceForAssessment(item=item, text=item.snippet, fully_fetched=False)]
    assessment = Mock()
    assessment.assess.return_value = Mock(
        data=SubmissionAssessmentOutput(
            assessments=[
                ClaimAssessmentOutput(
                    claim_id=str(claim_id), verdict="false", strength="strong",
                    reasons=AssessmentReasons(source_tiers=[2], independent_sources=1, conflicting_sources=False),
                    explanation="Demagog ocenił tezę jako fałsz.",
                    citations=[{"evidence_index": 0, "quote": item.snippet, "relation": "refutes"}],
                )
            ]
        )
    )
    pipeline = make_pipeline(
        run=VerificationRun(id=run_id, status=RunStatus.RUNNING, pipeline_version="t"),
        intake=intake, evidence_search=evidence_search, sublinks=sublinks, assessment=assessment,
    )

    result = asyncio.run(pipeline.run_url("https://example.org/a", channel=Channel.WEB))

    assert result.claims[0].assessment.verdict == "false"
    assert result.claims[0].evidence[0].item.url == item.url
    sublinks.select.assert_called_once_with("Rząd podniósł VAT.", [item])


def test_successful_outcomes_are_persisted(monkeypatch) -> None:
    run_id, claim_id = uuid.uuid4(), uuid.uuid4()
    intake = Mock()
    intake.submit_text.return_value = intake_result(
        RunStatus.OK, [stored(claim_id, "To moim zdaniem źle.", ClaimKind.OPINION)], run_id=run_id
    )
    evidence_search = Mock()
    evidence_search.search = AsyncMock(
        return_value=EvidenceSearchResult(claims=[], skipped_opinion_ids=[str(claim_id)])
    )
    record_outcome = Mock()
    monkeypatch.setattr("prawdowid.persistence.evidence.record_outcome", record_outcome)
    pipeline = make_pipeline(
        run=VerificationRun(id=run_id, status=RunStatus.RUNNING, pipeline_version="t"),
        intake=intake, evidence_search=evidence_search,
    )

    result = asyncio.run(pipeline.run_text("To moim zdaniem źle.", channel=Channel.WEB))

    assert result.status == RunStatus.OK
    record_outcome.assert_called_once()
    assert record_outcome.call_args.kwargs["claim_id"] == claim_id
    assert record_outcome.call_args.kwargs["run_id"] == run_id


def test_persistence_failure_marks_the_run_tech_failure(monkeypatch) -> None:
    run_id, claim_id = uuid.uuid4(), uuid.uuid4()
    intake = Mock()
    intake.submit_text.return_value = intake_result(
        RunStatus.OK, [stored(claim_id, "To moim zdaniem źle.", ClaimKind.OPINION)], run_id=run_id
    )
    evidence_search = Mock()
    evidence_search.search = AsyncMock(
        return_value=EvidenceSearchResult(claims=[], skipped_opinion_ids=[str(claim_id)])
    )
    monkeypatch.setattr(
        "prawdowid.persistence.evidence.record_outcome", Mock(side_effect=RuntimeError("db down"))
    )
    run = VerificationRun(id=run_id, status=RunStatus.RUNNING, pipeline_version="t")
    pipeline = make_pipeline(run=run, intake=intake, evidence_search=evidence_search)

    result = asyncio.run(pipeline.run_text("To moim zdaniem źle.", channel=Channel.WEB))

    assert result.status == RunStatus.TECH_FAILURE
    assert run.status == RunStatus.TECH_FAILURE


def test_evidence_search_failure_marks_the_run_tech_failure() -> None:
    run_id, claim_id = uuid.uuid4(), uuid.uuid4()
    intake = Mock()
    intake.submit_text.return_value = intake_result(RunStatus.OK, [stored(claim_id, "Teza.")], run_id=run_id)
    evidence_search = Mock()
    evidence_search.search = AsyncMock(side_effect=RuntimeError("outage"))
    run = VerificationRun(id=run_id, status=RunStatus.RUNNING, pipeline_version="t")
    pipeline = make_pipeline(run=run, intake=intake, evidence_search=evidence_search)

    result = asyncio.run(pipeline.run_text("Teza.", channel=Channel.WEB))

    assert result.status == RunStatus.TECH_FAILURE
    assert result.claims == []
    assert run.status == RunStatus.TECH_FAILURE


def test_assessment_failure_marks_the_run_tech_failure() -> None:
    run_id, claim_id = uuid.uuid4(), uuid.uuid4()
    intake = Mock()
    intake.submit_text.return_value = intake_result(RunStatus.OK, [stored(claim_id, "Teza.")], run_id=run_id)
    item = evidence_item()
    evidence_search = Mock()
    evidence_search.search = AsyncMock(
        return_value=EvidenceSearchResult(
            claims=[ClaimEvidence(claim_id=str(claim_id), items=[item])], skipped_opinion_ids=[],
        )
    )
    sublinks = Mock()
    sublinks.select.return_value = [EvidenceForAssessment(item=item, text=item.snippet, fully_fetched=False)]
    assessment = Mock()
    assessment.assess.side_effect = RuntimeError("model unavailable")
    run = VerificationRun(id=run_id, status=RunStatus.RUNNING, pipeline_version="t")
    pipeline = make_pipeline(
        run=run, intake=intake, evidence_search=evidence_search, sublinks=sublinks, assessment=assessment
    )

    result = asyncio.run(pipeline.run_text("Teza.", channel=Channel.WEB))

    assert result.status == RunStatus.TECH_FAILURE
    assert run.status == RunStatus.TECH_FAILURE
