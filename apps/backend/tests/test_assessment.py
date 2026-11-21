from datetime import date
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
    GENERAL_KNOWLEDGE_NOTE,
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
    assert "EUvsDisinfo" in assessment.limitations


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


def test_quote_differing_only_in_whitespace_is_accepted_and_replaced_by_the_source_span() -> None:
    source_text = "Wrocław nie jest jedynym\xa0miastem.\n\nZ\xa0takiej ulgi korzystają też inni."
    llm = Mock()
    llm.generate_structured.return_value = output(
        citations=[{"evidence_index": 0, "quote": "Wrocław nie jest jedynym miastem. Z takiej ulgi", "relation": "refutes"}]
    )
    target = ClaimEvidenceBundle(
        claim_id="c1", claim_text="Teza.", claim_kind="factual_claim", evidence=[evidence(source_text)]
    )

    result = AssessmentGenerator(llm=llm).assess([target])

    assert result.data.assessments[0].citations[0].quote == "Wrocław nie jest jedynym\xa0miastem.\n\nZ\xa0takiej ulgi"
    assert llm.generate_structured.call_count == 1


def test_quote_with_changed_words_is_still_rejected() -> None:
    llm = Mock()
    llm.generate_structured.return_value = output(
        citations=[{"evidence_index": 0, "quote": "Demagog ocenił tezę jako prawdę.", "relation": "refutes"}]
    )

    with pytest.raises(RuntimeError, match="absent"):
        AssessmentGenerator(llm=llm).assess([bundle()])


def announcement_bundle(claim_text: str) -> ClaimEvidenceBundle:
    return ClaimEvidenceBundle(
        claim_id="c1", claim_text=claim_text, claim_kind="verification_question",
        evidence=[evidence("W dniu 12 października 2026 r. w Belgii odbędzie się ogólnokrajowa akcja strajkowa.")],
    )


def confirmed_by(quote: str) -> StructuredResult[SubmissionAssessmentOutput]:
    return output(
        verdict="confirmed", explanation="Strajk się odbył.",
        citations=[{"evidence_index": 0, "quote": quote, "relation": "supports"}],
    )


def test_past_event_confirmed_only_by_an_announcement_becomes_insufficient_evidence() -> None:
    llm = Mock()
    llm.generate_structured.return_value = confirmed_by("w Belgii odbędzie się ogólnokrajowa akcja strajkowa")

    result = AssessmentGenerator(llm=llm).assess([announcement_bundle("Czy 12 października w Belgii był strajk?")])

    assessment = result.data.assessments[0]
    assert assessment.verdict == "insufficient_evidence"
    assert assessment.citations[0].relation == "adds_context"
    assert assessment.explanation.startswith("Nie wiem.")


def test_confirming_that_an_event_was_announced_is_left_alone() -> None:
    llm = Mock()
    llm.generate_structured.return_value = confirmed_by("w Belgii odbędzie się ogólnokrajowa akcja strajkowa")

    result = AssessmentGenerator(llm=llm).assess(
        [announcement_bundle("Na 12 października w Belgii zapowiedziano strajk.")]
    )

    assert result.data.assessments[0].verdict == "confirmed"


def general_bundle(*, allowed: bool, with_evidence: bool = False) -> ClaimEvidenceBundle:
    return ClaimEvidenceBundle(
        claim_id="c1", claim_text="Bitwa pod Grunwaldem była w 1410 roku.", claim_kind="factual_claim",
        evidence=[evidence()] if with_evidence else [], general_knowledge_allowed=allowed,
    )


def general_answer(**kwargs) -> StructuredResult[SubmissionAssessmentOutput]:
    return output(
        verdict="confirmed", strength="strong", explanation="Tak, bitwa odbyła się 15 lipca 1410 roku.", citations=[],
        reasons={"source_tiers": [], "independent_sources": 0, "conflicting_sources": False, **kwargs},
    )


def test_general_knowledge_answer_is_kept_but_always_weak_and_labelled() -> None:
    llm = Mock()
    llm.generate_structured.return_value = general_answer(basis="general_knowledge")

    assessment = AssessmentGenerator(llm=llm).assess([general_bundle(allowed=True)]).data.assessments[0]

    assert assessment.verdict == "confirmed"
    assert assessment.strength == "weak"
    assert assessment.reasons.basis == "general_knowledge"
    assert assessment.limitations == GENERAL_KNOWLEDGE_NOTE
    assert '"general_knowledge_allowed": true' in llm.generate_structured.call_args.args[0]


def test_general_knowledge_is_refused_for_claims_not_marked_as_such() -> None:
    llm = Mock()
    llm.generate_structured.return_value = general_answer(basis="general_knowledge")

    assessment = AssessmentGenerator(llm=llm).assess(
        [general_bundle(allowed=False, with_evidence=True)]
    ).data.assessments[0]

    assert assessment.verdict == "insufficient_evidence"
    assert assessment.reasons.basis == "evidence"
    assert assessment.explanation == "Nie wiem. Nie znalazłem wiarygodnych informacji na ten temat."


def test_uncited_verdict_with_nothing_to_cite_is_treated_as_general_knowledge() -> None:
    llm = Mock()
    llm.generate_structured.return_value = general_answer()  # model forgot to set basis

    assessment = AssessmentGenerator(llm=llm).assess([general_bundle(allowed=True)]).data.assessments[0]

    assert assessment.reasons.basis == "general_knowledge"
    assert assessment.strength == "weak"


def test_missing_details_are_kept_only_when_there_is_no_verdict() -> None:
    llm = Mock()
    llm.generate_structured.return_value = output(missing_details=["Kiedy dostałeś tę wiadomość?"])

    assessment = AssessmentGenerator(llm=llm).assess([bundle()]).data.assessments[0]

    assert assessment.verdict == "false"
    assert assessment.missing_details == []

    llm.generate_structured.return_value = output(
        verdict="insufficient_evidence", strength="weak", citations=[],
        explanation="Nie wiem – potrzebuję więcej szczegółów.",
        missing_details=["Kiedy dostałeś tę wiadomość?", " ", "O który bank chodzi?", "Gdzie?", "Kto?"],
    )

    assessment = AssessmentGenerator(llm=llm).assess([bundle()]).data.assessments[0]

    assert assessment.missing_details == ["Kiedy dostałeś tę wiadomość?", "O który bank chodzi?", "Gdzie?"]


def test_no_evidence_with_missing_details_asks_the_user_to_clarify() -> None:
    claim = ClaimForVerification(
        id="c1", text="Banki już blokują konta.", kind="factual_claim",
        ambiguities=["Nie sprecyzowano, o które banki chodzi."],
    )

    assessment = no_evidence_assessment(claim)

    assert assessment.explanation.startswith("Nie wiem")
    assert assessment.missing_details == ["Nie sprecyzowano, o które banki chodzi."]


def test_prompt_states_today_so_the_model_can_reason_about_the_present() -> None:
    llm = Mock()
    llm.generate_structured.return_value = output()

    AssessmentGenerator(llm=llm, today=lambda: date(2026, 11, 21)).assess([bundle()])

    assert '"today": "2026-11-21"' in llm.generate_structured.call_args.args[0]
