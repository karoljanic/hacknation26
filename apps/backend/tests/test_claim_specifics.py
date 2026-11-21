import pytest

from prawdowid.persistence.claims import same_specifics


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("VAT wynosi 23%.", "Podatek VAT wynosi 23 %"),
        ("Inflacja wyniosła 3,6%.", "Inflacja wyniosła 3.6%"),
        ("Rząd nie podniósł VAT.", "Rząd nigdy nie podniósł VAT"),  # negative concord: still one negation
        ("Czy Wrocław ma darmowe przejazdy?", "Czy we Wrocławiu są darmowe przejazdy?"),
    ],
)
def test_claims_with_same_numbers_and_polarity_may_be_merged(first: str, second: str) -> None:
    assert same_specifics(first, second)


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("VAT wynosi 23%.", "VAT wynosi 25%."),
        ("Inflacja wyniosła 3,6%.", "Inflacja wyniosła 3,7%."),
        ("Rząd podniósł VAT.", "Rząd nie podniósł VAT."),
        ("Zdarzenie miało miejsce w 2022 roku.", "Zdarzenie miało miejsce w 2023 roku."),
        ("W protestach brało udział 100 osób.", "W protestach brało udział 100 tysięcy osób i 5 miast."),
    ],
)
def test_claims_differing_in_a_number_or_negation_are_never_merged(first: str, second: str) -> None:
    assert not same_specifics(first, second)
