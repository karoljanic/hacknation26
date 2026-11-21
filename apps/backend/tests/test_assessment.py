from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from prawdowid.ai.adapters.llm import StructuredResult
from prawdowid.domain.evidence import (
    ClaimEvidenceBundle,
    ClaimForVerification,
    EvidenceForAssessment,
    EvidenceItem,
    SubmissionAssessmentOutput,
)
from prawdowid.services.assessment import (
    AssessmentGenerator,
    no_evidence_assessment,
    opinion_assessment,
)


def evidence(text: str = "Demagog ocenił tezę jako fałsz.") -> EvidenceForAssessment:
    item = EvidenceItem(
        source="google_fact_check", kind="claim_review", title="Analiza", url="https://demagog.org.pl/a",
        publisher="Demagog", rating="Fałsz", snippet=text,
    )
    return EvidenceForAssessment(item=item, text=text, fully_fetched=True)


def euvsdisinfo_disinformation_evidence(text: str = "Ukraina wystrzeliła rakietę w stronę Polski.") -> EvidenceForAssessment:
    item = EvidenceItem(
        source="euvsdisinfo", kind="claim_review", title="Raport EUvsDisinfo", url="https://euvsdisinfo.eu/report/x",
        publisher="EUvsDisinfo", rating="Narracja uznana przez EUvsDisinfo za dezinformację", snippet=text,
    )
    return EvidenceForAssessment(item=item, text=text, fully_fetched=True)


def bundle(claim_id: str = "c1") -> ClaimEvidenceBundle:
    return ClaimEvidenceBundle(
        claim_id=claim_id, claim_text="Teza.", claim_kind="factual_claim", evidence=[evidence()]
    )


def output(**assessment_kwargs) -> StructuredResult[SubmissionAssessmentOutput]:
    base = {
        "claim_id": "c1", "verdict": "false", "strength": "strong",
        "reasons": {"source_tiers": [2], "independent_sources": 1, "conflicting_sources": False},
        "explanation": "Demagog ocenił tezę jako fałsz.",
        "citations": [{"evidence_index": 0, "quote": "Demagog ocenił tezę jako fałsz.", "relation": "refutes"}],
    }
    base.update(assessment_kwargs)
    data = SubmissionAssessmentOutput.model_validate({"assessments": [base]})
    return StructuredResult(data=data, requested_model="test", model_version="v1", input_tokens=1, output_tokens=1)


def test_valid_assessment_with_verified_citation_passes_through() -> None:
    llm = Mock()
    llm.generate_structured.return_value = output()

    result = AssessmentGenerator(llm=llm).assess([bundle()])

    assert result.data.assessments[0].verdict == "false"
    prompt = llm.generate_structured.call_args.args[0]
    assert "Demagog" in prompt


def test_fabricated_quote_is_rejected() -> None:
    llm = Mock()
    llm.generate_structured.return_value = output(
        citations=[{"evidence_index": 0, "quote": "Zdanie, którego nie ma w źródle.", "relation": "refutes"}]
    )

    with pytest.raises(RuntimeError, match="absent"):
        AssessmentGenerator(llm=llm).assess([bundle()])


def test_invalid_quote_is_retried_once_and_a_valid_answer_is_used() -> None:
    llm = Mock()
    llm.generate_structured.side_effect = [
        output(citations=[{"evidence_index": 0, "quote": "Parafraza, nie cytat.", "relation": "refutes"}]),
        output(),
    ]

    result = AssessmentGenerator(llm=llm).assess([bundle()])

    assert llm.generate_structured.call_count == 2
    assert result.data.assessments[0].citations[0].quote == "Demagog ocenił tezę jako fałsz."


def test_two_invalid_answers_still_fail_instead_of_returning_a_verdict() -> None:
    llm = Mock()
    llm.generate_structured.return_value = output(
        citations=[{"evidence_index": 0, "quote": "Parafraza, nie cytat.", "relation": "refutes"}]
    )

    with pytest.raises(RuntimeError, match="absent"):
        AssessmentGenerator(llm=llm).assess([bundle()])

    assert llm.generate_structured.call_count == 2


def test_definitive_verdict_without_citation_is_rejected() -> None:
    llm = Mock()
    llm.generate_structured.return_value = output(citations=[])

    with pytest.raises(RuntimeError, match="requires at least one citation"):
        AssessmentGenerator(llm=llm).assess([bundle()])


def test_out_of_range_evidence_index_is_rejected() -> None:
    llm = Mock()
    llm.generate_structured.return_value = output(citations=[{"evidence_index": 5, "quote": "x", "relation": "refutes"}])

    with pytest.raises(RuntimeError, match="unknown evidence"):
        AssessmentGenerator(llm=llm).assess([bundle()])


def test_missing_claim_in_response_is_rejected() -> None:
    llm = Mock()
    data = SubmissionAssessmentOutput.model_validate({"assessments": []})
    llm.generate_structured.return_value = StructuredResult(
        data=data, requested_model="t", model_version=None, input_tokens=None, output_tokens=None
    )

    with pytest.raises(RuntimeError, match="exactly the submitted claims"):
        AssessmentGenerator(llm=llm).assess([bundle()])


def test_injection_note_must_quote_real_evidence_text() -> None:
    llm = Mock()
    data = SubmissionAssessmentOutput.model_validate(
        {
            **output().data.model_dump(),
            "injection_notes": [{"claim_id": "c1", "evidence_index": 0, "excerpt": "nie ma takiego tekstu"}],
        }
    )
    llm.generate_structured.return_value = StructuredResult(
        data=data, requested_model="t", model_version=None, input_tokens=None, output_tokens=None
    )

    with pytest.raises(RuntimeError, match="Injection note"):
        AssessmentGenerator(llm=llm).assess([bundle()])


def test_empty_bundle_list_is_rejected() -> None:
    with pytest.raises(ValueError, match="At least one"):
        AssessmentGenerator(llm=Mock()).assess([])


def test_duplicate_claim_ids_are_rejected() -> None:
    with pytest.raises(ValueError, match="unique"):
        AssessmentGenerator(llm=Mock()).assess([bundle("c1"), bundle("c1")])


def test_opinion_assessment_is_deterministic_and_unverifiable() -> None:
    claim = ClaimForVerification(id="c1", text="Uważam, że to źle.", kind="opinion")

    assessment = opinion_assessment(claim)

    assert assessment.verdict == "unverifiable"
    assert assessment.citations == []


def test_no_evidence_assessment_abstains_and_reports_outages() -> None:
    claim = ClaimForVerification(id="c1", text="Nowa teza bez dowodów.", kind="factual_claim")

    assessment = no_evidence_assessment(claim, unavailable_sources=["euvsdisinfo"])

    assert assessment.verdict == "insufficient_evidence"
    assert "euvsdisinfo" in assessment.limitations


def test_confirmed_verdict_backed_only_by_euvsdisinfo_disinformation_is_corrected_to_false() -> None:
    evidence_item = euvsdisinfo_disinformation_evidence()
    disinfo_bundle = ClaimEvidenceBundle(
        claim_id="c1", claim_text="Teza.", claim_kind="factual_claim", evidence=[evidence_item]
    )
    llm = Mock()
    llm.generate_structured.return_value = output(
        verdict="confirmed",
        citations=[{"evidence_index": 0, "quote": evidence_item.text, "relation": "supports"}],
    )

    result = AssessmentGenerator(llm=llm).assess([disinfo_bundle])

    assessment = result.data.assessments[0]
    assert assessment.verdict == "false"
    assert assessment.citations[0].relation == "refutes"
    assert "EUvsDisinfo" in assessment.explanation


def test_false_verdict_backed_by_euvsdisinfo_disinformation_is_accepted() -> None:
    evidence_item = euvsdisinfo_disinformation_evidence()
    disinfo_bundle = ClaimEvidenceBundle(
        claim_id="c1", claim_text="Teza.", claim_kind="factual_claim", evidence=[evidence_item]
    )
    llm = Mock()
    llm.generate_structured.return_value = output(
        verdict="false",
        citations=[{"evidence_index": 0, "quote": evidence_item.text, "relation": "refutes"}],
    )

    result = AssessmentGenerator(llm=llm).assess([disinfo_bundle])

    assert result.data.assessments[0].verdict == "false"


def test_confirmed_verdict_with_mixed_citations_is_not_corrected() -> None:
    disinfo_item = euvsdisinfo_disinformation_evidence()
    genuine_item = evidence()
    mixed_bundle = ClaimEvidenceBundle(
        claim_id="c1", claim_text="Teza.", claim_kind="factual_claim", evidence=[disinfo_item, genuine_item]
    )
    llm = Mock()
    llm.generate_structured.return_value = output(
        verdict="confirmed",
        citations=[
            {"evidence_index": 0, "quote": disinfo_item.text, "relation": "adds_context"},
            {"evidence_index": 1, "quote": genuine_item.text, "relation": "supports"},
        ],
    )

    result = AssessmentGenerator(llm=llm).assess([mixed_bundle])

    assert result.data.assessments[0].verdict == "confirmed"


def test_reasons_reject_free_form_extra_fields() -> None:
    with pytest.raises(ValidationError):
        SubmissionAssessmentOutput.model_validate(
            {
                "assessments": [
                    {
                        "claim_id": "c1", "verdict": "false", "strength": "strong",
                        "reasons": {
                            "source_tiers": [2], "independent_sources": 1,
                            "conflicting_sources": False, "unexpected": "value",
                        },
                        "explanation": "x", "citations": [],
                    }
                ]
            }
        )
