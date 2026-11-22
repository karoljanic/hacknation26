from prawdowid.services.relevance import best_passages, claim_terms


def test_best_passages_returns_the_matching_parts_in_reading_order() -> None:
    text = (
        "Alert RCB o ataku z powietrza. " * 20
        + "Zatrucia tlenkiem węgla: od 1 października zmarło 8 osób. "
        + "Pogoda: przelotne opady śniegu. " * 20
    )

    passages = best_passages(text, claim_terms(["Ile osób zmarło z powodu zatrucia tlenkiem węgla?"]))

    assert passages is not None and passages.startswith("Zatrucia tlenkiem węgla: od 1 października zmarło 8 osób.")
    assert "Alert RCB" not in passages


def test_best_passages_is_none_when_nothing_matches() -> None:
    assert best_passages("Komunikat o pogodzie.", claim_terms(["Strajk nauczycieli"])) is None
