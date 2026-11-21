"""Masking of obvious personal data before text is stored or sent to a model.

Deliberately conservative regexes for the clearest identifiers only; names and
addresses are *not* detected. Order matters: longer number formats first.
"""

import re

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("[EMAIL]", re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")),
    # Polish IBAN / NRB: optional "PL", 26 digits, optional spaces in groups.
    ("[NR_KONTA]", re.compile(r"\b(?:PL ?)?\d{2}(?: ?\d{4}){6}\b", re.IGNORECASE)),
    # Payment card: 13-19 digits, optionally grouped by spaces/dashes.
    ("[NR_KARTY]", re.compile(r"\b\d{4}(?:[ -]?\d{4}){2}[ -]?\d{1,7}\b")),
    ("[PESEL]", re.compile(r"\b\d{11}\b")),
    # Phone with an explicit +48/0048 prefix, any common grouping.
    ("[TELEFON]", re.compile(r"(?<![\w+])(?:\+|00)48[ -]?\d{3}[ -]?\d{3}[ -]?\d{3}\b")),
    # Phone without prefix only as 9 contiguous or dash-grouped digits.
    # Space-grouped "300 000 000" is NOT masked: it is far more often an amount
    # in a claim than a phone number, and corrupting figures breaks fact-checks.
    ("[TELEFON]", re.compile(r"(?<![\w+-])(?:\d{9}|\d{3}-\d{3}-\d{3})(?![\w-])")),
)


def redact(text: str) -> tuple[str, int]:
    """Return the masked text and the number of replacements made."""
    total = 0
    for placeholder, pattern in _PATTERNS:
        text, count = pattern.subn(placeholder, text)
        total += count
    return text, total
