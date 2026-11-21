from unittest.mock import Mock

from prawdowid.ai.adapters.decision import DecisionResult, ScoreAnswer
from prawdowid.domain.evidence import ClaimForVerification
from prawdowid.services.claim_selection import ClaimSelector


def claims(count: int) -> list[ClaimForVerification]:
    return [ClaimForVerification(id=f"c{i}", text=f"Teza {i}.", kind="factual_claim") for i in range(count)]


def scored(values: list[float]) -> DecisionResult:
    return DecisionResult(
        model="test", input_tokens=1, output_tokens=1,
        answers={
            str(i): ScoreAnswer(type="score", score=value, confidence=0.9, probabilities={}, legend={})
            for i, value in enumerate(values)
        },
    )


def test_few_claims_are_all_checked_without_a_decision_call() -> None:
    decision = Mock()

    selection = ClaimSelector(decision=decision).select("Zgłoszenie.", claims(3))

    assert selection.selected == ["c0", "c1", "c2"]
    decision.decide.assert_not_called()


def test_only_key_claims_are_kept_up_to_the_limit_in_original_order() -> None:
    decision = Mock()
    decision.decide.return_value = scored([1.0, 2.9, 0.2, 2.5, 2.1, 2.2])

    selection = ClaimSelector(decision=decision, max_claims=3).select("Zgłoszenie.", claims(6))

    assert selection.selected == ["c1", "c3", "c5"]
    assert selection.skipped == ["c0", "c2", "c4"]
    assert '"submission": "Zgłoszenie."' in decision.decide.call_args.args[0]


def test_the_most_central_claim_is_kept_even_if_none_scores_as_key() -> None:
    decision = Mock()
    decision.decide.return_value = scored([0.4, 1.6, 0.9, 1.1])

    selection = ClaimSelector(decision=decision).select("Zgłoszenie.", claims(4))

    assert selection.selected == ["c1"]
