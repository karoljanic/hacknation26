from unittest.mock import Mock

import pytest

from prawdowid.ai.adapters.decision import BooleanAnswer, DecisionResult
from prawdowid.domain.evidence import EvidenceItem
from prawdowid.services.sublinks import SublinkSelector


def item(url: str, title: str = "Tytuł") -> EvidenceItem:
    return EvidenceItem(
        source="google_fact_check", kind="claim_review", title=title, url=url,
        publisher="Demagog", rating="Fałsz", snippet="Krótki fragment.",
    )


def decision_result(scores: list[float]) -> DecisionResult:
    return DecisionResult(
        model="test", input_tokens=1, output_tokens=1,
        answers={str(i): BooleanAnswer(type="noul", noul=score) for i, score in enumerate(scores)},
    )


def test_selected_items_get_full_text_others_keep_snippet() -> None:
    items = [item("https://demagog.org.pl/a"), item("https://demagog.org.pl/b")]
    decision = Mock()
    decision.decide.return_value = decision_result([0.9, 0.1])
    preparer = Mock()
    preparer.prepare_url.return_value = Mock(segments=[Mock(text="Pełna treść artykułu.")])

    bundle = SublinkSelector(decision=decision, preparer=preparer, threshold=0.5).select("Teza.", items)

    assert bundle[0].fully_fetched is True
    assert bundle[0].text == "Pełna treść artykułu."
    assert bundle[1].fully_fetched is False
    assert bundle[1].text == "Krótki fragment."
    preparer.prepare_url.assert_called_once_with("https://demagog.org.pl/a")


def test_fetch_failure_falls_back_to_snippet_without_raising() -> None:
    items = [item("https://demagog.org.pl/a")]
    decision = Mock()
    decision.decide.return_value = decision_result([0.9])
    preparer = Mock()
    preparer.prepare_url.side_effect = ValueError("blocked")

    bundle = SublinkSelector(decision=decision, preparer=preparer, threshold=0.5).select("Teza.", items)

    assert bundle[0].fully_fetched is False
    assert bundle[0].text == "Krótki fragment."


def test_max_selected_is_respected() -> None:
    items = [item(f"https://demagog.org.pl/{i}") for i in range(4)]
    decision = Mock()
    decision.decide.return_value = decision_result([0.9, 0.9, 0.9, 0.9])
    preparer = Mock()
    preparer.prepare_url.return_value = Mock(segments=[Mock(text="Pełna treść.")])

    SublinkSelector(decision=decision, preparer=preparer, max_selected=2, threshold=0.5).select("Teza.", items)

    assert preparer.prepare_url.call_count == 2


def test_no_candidates_above_threshold_fetches_nothing() -> None:
    items = [item("https://demagog.org.pl/a")]
    decision = Mock()
    decision.decide.return_value = decision_result([0.2])
    preparer = Mock()

    bundle = SublinkSelector(decision=decision, preparer=preparer, threshold=0.5).select("Teza.", items)

    assert bundle[0].fully_fetched is False
    preparer.prepare_url.assert_not_called()


def test_empty_items_short_circuits_without_calling_decision() -> None:
    decision = Mock()
    preparer = Mock()

    assert SublinkSelector(decision=decision, preparer=preparer).select("Teza.", []) == []
    decision.decide.assert_not_called()


def test_invalid_bounds_are_rejected() -> None:
    with pytest.raises(ValueError):
        SublinkSelector(decision=Mock(), preparer=Mock(), max_candidates=1, max_selected=2)


def test_odyseusz_items_are_never_fetched_and_count_as_full_text() -> None:
    advisory = EvidenceItem(
        source="odyseusz", kind="official", title="Komunikat MSZ", url="https://odyseusz.gov.pl/IT",
        snippet="Pełny komunikat MSZ.",
    )
    items = [advisory, item("https://demagog.org.pl/a")]
    decision = Mock()
    decision.decide.return_value = decision_result([0.9])
    preparer = Mock()
    preparer.prepare_url.return_value = Mock(segments=[Mock(text="Pełna treść artykułu.")])

    bundle = SublinkSelector(decision=decision, preparer=preparer, threshold=0.5).select("Teza.", items)

    preparer.prepare_url.assert_called_once_with("https://demagog.org.pl/a")
    assert len(decision.decide.call_args.args[1]) == 1
    assert bundle[0].fully_fetched is True and bundle[0].text == "Pełny komunikat MSZ."
    assert bundle[1].text == "Pełna treść artykułu."


def test_without_a_decision_provider_snippets_are_used() -> None:
    items = [item("https://demagog.org.pl/a")]
    preparer = Mock()

    bundle = SublinkSelector(decision=None, preparer=preparer).select("Teza.", items)

    assert [entry.text for entry in bundle] == ["Krótki fragment."]
    preparer.prepare_url.assert_not_called()


def test_decision_failure_falls_back_to_snippets_without_raising() -> None:
    items = [item("https://demagog.org.pl/a")]
    decision = Mock()
    decision.decide.side_effect = RuntimeError("TypeSafe down")

    bundle = SublinkSelector(decision=decision, preparer=Mock()).select("Teza.", items)

    assert bundle[0].fully_fetched is False


def test_http_error_on_fetch_falls_back_to_snippet() -> None:
    items = [item("https://demagog.org.pl/a")]
    decision = Mock()
    decision.decide.return_value = decision_result([0.9])
    preparer = Mock()
    preparer.prepare_url.side_effect = RuntimeError("Request failed with HTTP 403")

    bundle = SublinkSelector(decision=decision, preparer=preparer, threshold=0.5).select("Teza.", items)

    assert bundle[0].fully_fetched is False
