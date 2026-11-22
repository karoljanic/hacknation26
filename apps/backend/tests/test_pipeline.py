import asyncio
import dataclasses
import uuid
from unittest.mock import AsyncMock, Mock

import pytest

from prawdowid.ai.adapters.llm import ContentBlocked
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
from prawdowid.services.claim_selection import ClaimSelection
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
    assert "EUvsDisinfo" in result.claims[0].assessment.limitations
    assessment.assess.assert_not_called()


def test_general_knowledge_claim_without_evidence_still_reaches_the_model() -> None:
    run_id, claim_id = uuid.uuid4(), uuid.uuid4()
    intake = Mock()
    general = dataclasses.replace(stored(claim_id, "Bitwa pod Grunwaldem była w 1410 roku."), general_knowledge=True)
    intake.submit_text.return_value = intake_result(RunStatus.OK, [general], run_id=run_id)
    evidence_search = Mock()
    evidence_search.search = AsyncMock(
        return_value=EvidenceSearchResult(claims=[ClaimEvidence(claim_id=str(claim_id), items=[])], skipped_opinion_ids=[])
    )
    sublinks = Mock()
    sublinks.select.return_value = []
    assessment = Mock()
    assessment.assess.return_value = Mock(
        requested_model="m",
        data=SubmissionAssessmentOutput(
            assessments=[
                ClaimAssessmentOutput(
                    claim_id=str(claim_id), verdict="confirmed", strength="weak",
                    reasons=AssessmentReasons(
                        source_tiers=[], independent_sources=0, conflicting_sources=False, basis="general_knowledge"
                    ),
                    explanation="Tak, w 1410 roku.", citations=[],
                )
            ]
        ),
    )
    pipeline = make_pipeline(
        run=VerificationRun(id=run_id, status=RunStatus.RUNNING, pipeline_version="t"),
        intake=intake, evidence_search=evidence_search, sublinks=sublinks, assessment=assessment,
    )

    result = asyncio.run(pipeline.run_text("Bitwa pod Grunwaldem była w 1410 roku.", channel=Channel.WEB))

    bundle = assessment.assess.call_args.args[0][0]
    assert bundle.evidence == [] and bundle.general_knowledge_allowed is True
    assert result.claims[0].assessment.reasons.basis == "general_knowledge"


def test_side_claims_are_skipped_and_reported_and_selector_outage_keeps_the_first_ones() -> None:
    run_id = uuid.uuid4()
    ids = [uuid.uuid4() for _ in range(5)]
    intake = Mock()
    intake.submit_text.return_value = intake_result(
        RunStatus.OK, [stored(claim_id, f"Teza {i}.") for i, claim_id in enumerate(ids)], run_id=run_id
    )
    evidence_search = Mock()
    evidence_search.search = AsyncMock(
        side_effect=lambda claims: EvidenceSearchResult(
            claims=[ClaimEvidence(claim_id=claim.id, items=[]) for claim in claims], skipped_opinion_ids=[]
        )
    )
    selector = Mock(max_claims=2)
    selector.select.return_value = ClaimSelection(
        selected=[str(ids[3])], skipped=[str(ids[i]) for i in (0, 1, 2, 4)]
    )
    pipeline = VerificationPipeline(
        intake=intake, evidence_search=evidence_search, sublinks=Mock(), assessment=Mock(),
        claim_selector=selector,
    )
    intake.session_factory = lambda: FakeSession({run_id: VerificationRun(id=run_id, status=RunStatus.RUNNING, pipeline_version="t")})

    result = asyncio.run(pipeline.run_text("Długa wiadomość.", channel=Channel.WEB))

    assert [outcome.text for outcome in result.claims] == ["Teza 3."]
    assert result.skipped_claims == ["Teza 0.", "Teza 1.", "Teza 2.", "Teza 4."]
    assert [claim.id for claim in evidence_search.search.call_args.args[0]] == [str(ids[3])]

    selector.select.side_effect = RuntimeError("decision outage")
    result = asyncio.run(pipeline.run_text("Długa wiadomość.", channel=Channel.WEB))

    assert result.status == RunStatus.OK
    assert [outcome.text for outcome in result.claims] == ["Teza 0.", "Teza 1."]
    assert len(result.skipped_claims) == 3


def test_only_open_questions_ask_the_user_to_rephrase_without_searching() -> None:
    run_id, claim_id = uuid.uuid4(), uuid.uuid4()
    run = VerificationRun(id=run_id, status=RunStatus.OK, pipeline_version="t")
    intake = Mock()
    question = dataclasses.replace(
        stored(claim_id, "Kiedy była bitwa pod Grunwaldem?", ClaimKind.VERIFICATION_QUESTION), open_question=True
    )
    intake.submit_text.return_value = intake_result(RunStatus.OK, [question], run_id=run_id)
    evidence_search = Mock()
    pipeline = make_pipeline(run=run, intake=intake, evidence_search=evidence_search)

    result = asyncio.run(pipeline.run_text("Kiedy była bitwa pod Grunwaldem?", channel=Channel.WEB, question=True))

    assert result.status == RunStatus.NEEDS_CLARIFICATION
    assert "„tak” lub „nie”" in result.message
    assert run.status == RunStatus.NEEDS_CLARIFICATION
    evidence_search.search.assert_not_called()


def test_open_question_next_to_a_checkable_claim_is_answered_deterministically() -> None:
    run_id, open_id, claim_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    intake = Mock()
    question = dataclasses.replace(
        stored(open_id, "Dlaczego to zrobili?", ClaimKind.VERIFICATION_QUESTION), open_question=True
    )
    intake.submit_text.return_value = intake_result(
        RunStatus.OK, [question, stored(claim_id, "Rząd podniósł VAT.")], run_id=run_id
    )
    evidence_search = Mock()
    evidence_search.search = AsyncMock(
        return_value=EvidenceSearchResult(claims=[ClaimEvidence(claim_id=str(claim_id), items=[])], skipped_opinion_ids=[])
    )
    pipeline = make_pipeline(
        run=VerificationRun(id=run_id, status=RunStatus.RUNNING, pipeline_version="t"),
        intake=intake, evidence_search=evidence_search,
    )

    result = asyncio.run(pipeline.run_text("Rząd podniósł VAT. Dlaczego to zrobili?", channel=Channel.WEB))

    assert [claim.id for claim in evidence_search.search.call_args.args[0]] == [str(claim_id)]
    verdicts = {outcome.text: outcome.assessment.verdict for outcome in result.claims}
    assert verdicts == {"Dlaczego to zrobili?": "unverifiable", "Rząd podniósł VAT.": "insufficient_evidence"}


def test_model_refusing_the_assessment_is_out_of_scope_not_a_technical_failure() -> None:
    run_id, claim_id = uuid.uuid4(), uuid.uuid4()
    intake = Mock()
    intake.submit_text.return_value = intake_result(RunStatus.OK, [stored(claim_id, "Nożownik zabił trzy osoby.")], run_id=run_id)
    item = evidence_item()
    evidence_search = Mock()
    evidence_search.search = AsyncMock(
        return_value=EvidenceSearchResult(claims=[ClaimEvidence(claim_id=str(claim_id), items=[item])], skipped_opinion_ids=[])
    )
    sublinks = Mock()
    sublinks.select.return_value = [EvidenceForAssessment(item=item, text=item.snippet, fully_fetched=False)]
    assessment = Mock()
    assessment.assess.side_effect = ContentBlocked("blocked")
    run = VerificationRun(id=run_id, status=RunStatus.RUNNING, pipeline_version="t")
    pipeline = make_pipeline(run=run, intake=intake, evidence_search=evidence_search, sublinks=sublinks, assessment=assessment)

    result = asyncio.run(pipeline.run_text("Nożownik zabił trzy osoby.", channel=Channel.WEB))

    assert result.status == RunStatus.OUT_OF_SCOPE
    assert "nie jest ocena prawdziwości" in result.message
    assert run.status == RunStatus.OUT_OF_SCOPE


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


def _assessed_with_injection_note(claim_id: uuid.UUID, item: EvidenceItem):
    from prawdowid.ai.adapters.llm import StructuredResult

    data = SubmissionAssessmentOutput(
        assessments=[
            ClaimAssessmentOutput(
                claim_id=str(claim_id), verdict="false", strength="strong",
                reasons=AssessmentReasons(source_tiers=[2], independent_sources=1, conflicting_sources=False),
                explanation="Demagog ocenił tezę jako fałsz.",
                citations=[{"evidence_index": 0, "quote": item.snippet, "relation": "refutes"}],
            )
        ],
        injection_notes=[{"claim_id": str(claim_id), "evidence_index": 0, "excerpt": "Demagog ocenił"}],
    )
    return StructuredResult(data=data, requested_model="gemini-test", model_version="v1",
                            input_tokens=1000, output_tokens=100)


def test_run_is_closed_once_after_the_verdicts_with_full_telemetry(monkeypatch) -> None:
    from datetime import datetime, timedelta, timezone

    from prawdowid.services.usage import UsageMeter, usage_entry

    monkeypatch.setenv("MODEL_PRICES_USD_PER_MTOK", '{"gemini-test": [1, 10], "jev": [0, 0]}')
    run_id, claim_id = uuid.uuid4(), uuid.uuid4()
    intake = Mock()
    intake.submit_text.return_value = intake_result(RunStatus.OK, [stored(claim_id, "Teza.")], run_id=run_id)
    item = evidence_item()
    evidence_search = Mock()
    evidence_search.search = AsyncMock(
        return_value=EvidenceSearchResult(claims=[ClaimEvidence(claim_id=str(claim_id), items=[item])], skipped_opinion_ids=[])
    )
    meter = UsageMeter()
    sublinks = Mock()

    def select(text, items):
        meter.record(usage_entry("decision", "jev", None, 50, 5))
        return [EvidenceForAssessment(item=item, text=item.snippet, fully_fetched=False)]

    sublinks.select.side_effect = select
    assessment = Mock()
    assessment.assess.return_value = _assessed_with_injection_note(claim_id, item)
    # As left by intake with keep_run_open: still running, extraction usage recorded.
    run = VerificationRun(
        id=run_id, status=RunStatus.RUNNING, pipeline_version="t",
        started_at=datetime.now(timezone.utc) - timedelta(seconds=2),
        models=[usage_entry("extraction", "gemini-test", "v1", 1000, 0)], injection_flags=[],
    )
    pipeline = make_pipeline(run=run, intake=intake, evidence_search=evidence_search, sublinks=sublinks,
                             assessment=assessment)
    pipeline.usage_meter = meter

    result = asyncio.run(pipeline.run_text("Teza.", channel=Channel.WEB))

    assert result.status == RunStatus.OK
    assert run.status == RunStatus.OK and run.finished_at is not None
    assert run.latency_ms >= 2000  # end-to-end, not just intake
    assert [m["role"] for m in run.models] == ["extraction", "decision", "assessment"]
    # (1000 + 1000) input * $1/M + 100 output * $10/M
    assert str(run.est_cost_usd) == "0.003000"
    assert run.injection_flags == [{
        "where": str(item.url), "claim_id": str(claim_id), "excerpt": "Demagog ocenił",
        "detected_by": "assessment_model",
    }]


def test_unexpected_failure_still_closes_the_run() -> None:
    run_id, claim_id = uuid.uuid4(), uuid.uuid4()
    intake = Mock()
    intake.submit_text.return_value = intake_result(RunStatus.OK, [stored(claim_id, "Teza.")], run_id=run_id)
    item = evidence_item()
    evidence_search = Mock()
    evidence_search.search = AsyncMock(
        return_value=EvidenceSearchResult(claims=[ClaimEvidence(claim_id=str(claim_id), items=[item])], skipped_opinion_ids=[])
    )
    sublinks = Mock()
    sublinks.select.side_effect = KeyError("bug")
    run = VerificationRun(id=run_id, status=RunStatus.RUNNING, pipeline_version="t")
    pipeline = make_pipeline(run=run, intake=intake, evidence_search=evidence_search, sublinks=sublinks)

    result = asyncio.run(pipeline.run_text("Teza.", channel=Channel.WEB))

    assert result.status == RunStatus.TECH_FAILURE
    assert run.status == RunStatus.TECH_FAILURE and run.finished_at is not None


def test_cancellation_closes_the_run_and_propagates() -> None:
    run_id, claim_id = uuid.uuid4(), uuid.uuid4()
    intake = Mock()
    intake.submit_text.return_value = intake_result(RunStatus.OK, [stored(claim_id, "Teza.")], run_id=run_id)
    evidence_search = Mock()
    evidence_search.search = AsyncMock(side_effect=asyncio.CancelledError())
    run = VerificationRun(id=run_id, status=RunStatus.RUNNING, pipeline_version="t")
    pipeline = make_pipeline(run=run, intake=intake, evidence_search=evidence_search)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(pipeline.run_text("Teza.", channel=Channel.WEB))

    assert run.status == RunStatus.TECH_FAILURE and run.finished_at is not None
    assert "anulowane" in run.error


def decision_scoring(score: float) -> Mock:
    from prawdowid.ai.adapters.decision import BooleanAnswer, DecisionResult

    decision = Mock()
    decision.decide.side_effect = lambda state, questions: DecisionResult(
        model="jev", input_tokens=1, output_tokens=1,
        answers={key: BooleanAnswer(type="noul", noul=score) for key in questions},
    )
    return decision


def cached_answer(item: EvidenceItem) -> dict:
    from prawdowid.services.answer_cache import snapshot_of

    assessment = ClaimAssessmentOutput(
        claim_id="old", verdict="false", strength="strong",
        reasons=AssessmentReasons(source_tiers=[2], independent_sources=1, conflicting_sources=False),
        explanation="Demagog ocenił tezę jako fałsz.",
        citations=[{"evidence_index": 1, "quote": item.snippet, "relation": "refutes"}],
    )
    unrelated = evidence_item("https://example.org/inne")
    return snapshot_of(assessment, [EvidenceForAssessment(item=unrelated, text="x", fully_fetched=False),
                                    EvidenceForAssessment(item=item, text=item.snippet, fully_fetched=False)])


@pytest.mark.parametrize("time_score, reused", [(0.1, True), (0.9, False)])
def test_a_valid_stored_answer_is_reused_unless_time_critical(monkeypatch, time_score: float, reused: bool) -> None:
    run_id, claim_id = uuid.uuid4(), uuid.uuid4()
    known = dataclasses.replace(stored(claim_id, "Rząd podniósł VAT."), match="exact")
    intake = Mock()
    intake.submit_text.return_value = intake_result(RunStatus.OK, [known], run_id=run_id)
    item = evidence_item()
    monkeypatch.setattr("prawdowid.services.pipeline.latest_valid", lambda session, cid, now: (uuid.uuid4(), cached_answer(item)))
    evidence_search = Mock()
    evidence_search.search = AsyncMock(
        return_value=EvidenceSearchResult(claims=[ClaimEvidence(claim_id=str(claim_id), items=[])], skipped_opinion_ids=[])
    )
    pipeline = make_pipeline(
        run=VerificationRun(id=run_id, status=RunStatus.RUNNING, pipeline_version="t"),
        intake=intake, evidence_search=evidence_search,
    )
    pipeline.cache_decision = decision_scoring(time_score)

    result = asyncio.run(pipeline.run_text("Rząd podniósł VAT.", channel=Channel.WEB))

    [outcome] = result.claims
    searched = evidence_search.search.call_args.args[0]
    if reused:
        assert searched == [] and outcome.judged_by.startswith("cache:")
        assert outcome.assessment.verdict == "false" and outcome.assessment.claim_id == str(claim_id)
        assert outcome.evidence[outcome.assessment.citations[0].evidence_index].item.url == item.url
    else:
        assert [claim.id for claim in searched] == [str(claim_id)]
        assert outcome.judged_by == "deterministic:no_evidence"


def test_model_answers_are_stored_for_a_day_with_their_cited_evidence(monkeypatch) -> None:
    from datetime import datetime, timedelta, timezone

    run_id, claim_id = uuid.uuid4(), uuid.uuid4()
    intake = Mock()
    intake.submit_text.return_value = intake_result(RunStatus.OK, [stored(claim_id, "Rząd podniósł VAT.")], run_id=run_id)
    item = evidence_item()
    evidence_search = Mock()
    evidence_search.search = AsyncMock(
        return_value=EvidenceSearchResult(claims=[ClaimEvidence(claim_id=str(claim_id), items=[item])], skipped_opinion_ids=[])
    )
    sublinks = Mock()
    sublinks.select.return_value = [EvidenceForAssessment(item=item, text=item.snippet, fully_fetched=False)]
    assessment = Mock()
    assessment.assess.return_value = Mock(
        requested_model="m", model_version="v", input_tokens=1, output_tokens=1,
        data=SubmissionAssessmentOutput(assessments=[ClaimAssessmentOutput(
            claim_id=str(claim_id), verdict="false", strength="strong",
            reasons=AssessmentReasons(source_tiers=[2], independent_sources=1, conflicting_sources=False),
            explanation="Demagog ocenił tezę jako fałsz.",
            citations=[{"evidence_index": 0, "quote": item.snippet, "relation": "refutes"}],
        )]),
    )
    record_outcome = Mock()
    monkeypatch.setattr("prawdowid.persistence.evidence.record_outcome", record_outcome)
    pipeline = make_pipeline(
        run=VerificationRun(id=run_id, status=RunStatus.RUNNING, pipeline_version="t"),
        intake=intake, evidence_search=evidence_search, sublinks=sublinks, assessment=assessment,
    )

    asyncio.run(pipeline.run_text("Rząd podniósł VAT.", channel=Channel.WEB))

    kwargs = record_outcome.call_args.kwargs
    assert abs(kwargs["valid_until"] - (datetime.now(timezone.utc) + timedelta(days=1))) < timedelta(minutes=1)
    assert kwargs["snapshot"]["evidence"][0]["item"]["url"] == str(item.url)
