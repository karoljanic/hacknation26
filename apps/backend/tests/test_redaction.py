import pytest

from prawdowid.persistence.claims import claim_text_hash, normalize_claim_text
from prawdowid.security.redaction import redact


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Pisz na jan.kowalski@example.pl", "Pisz na [EMAIL]"),
        ("Dzwoń +48 600 123 456 teraz", "Dzwoń [TELEFON] teraz"),
        ("tel. 600123456", "tel. [TELEFON]"),
        ("tel. 600-123-456", "tel. [TELEFON]"),
        ("PESEL 44051401359", "PESEL [PESEL]"),
        ("konto PL61 1090 1014 0000 0712 1981 2874", "konto [NR_KONTA]"),
        ("karta 4111 1111 1111 1111", "karta [NR_KARTY]"),
    ],
)
def test_redacts_obvious_identifiers(text: str, expected: str) -> None:
    assert redact(text)[0] == expected


@pytest.mark.parametrize(
    "text",
    [
        "Rząd wydał 300 000 000 zł na program.",
        "Inflacja w 2024 roku wyniosła 3,6%.",
        "Ustawa z dnia 12.03.2025 r. weszła w życie.",
        "Polska ma 38 mln mieszkańców.",
    ],
)
def test_keeps_figures_that_claims_depend_on(text: str) -> None:
    assert redact(text) == (text, 0)


def test_claim_normalisation_ignores_case_spacing_and_final_punctuation() -> None:
    assert normalize_claim_text("  Inflacja  wyniosła\n3,6%. ") == "inflacja wyniosła 3,6%"
    assert claim_text_hash("Inflacja wyniosła 3,6%.") == claim_text_hash("inflacja wyniosła 3,6%")
    assert claim_text_hash("Inflacja wyniosła 3,6%") != claim_text_hash("Inflacja wyniosła 3,7%")
