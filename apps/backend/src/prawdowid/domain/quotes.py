"""Locating a model-reported quote in its source text.

Models reproduce quotes faithfully but not byte-for-byte: Polish typography
puts non-breaking spaces after one-letter words ("Z\\xa0takiej", "i\\xa0seniorki"),
pages use typographic quotes and dashes, and line breaks become spaces. An
exact ``quote in text`` check rejects such quotes, so matching ignores only
these presentation differences and then returns the *source's own* span,
which is what gets shown and stored: the quote stays verbatim.
"""

import unicodedata

_QUOTES = {ord(c): '"' for c in "„”“‟«»″"} | {ord(c): "'" for c in "’‘‚‛′"}
_DASHES = {ord(c): "-" for c in "‐‑‒–—―−"}
_FOLD = _QUOTES | _DASHES


def _normalize_with_map(text: str) -> tuple[str, list[int]]:
    """Folds whitespace runs to one space and unifies quotes/dashes; returns
    the folded text and, for each folded character, its index in ``text``."""
    chars: list[str] = []
    positions: list[int] = []
    previous_space = True  # also strips leading whitespace
    for index, char in enumerate(text):
        if char.isspace():
            if not previous_space:
                chars.append(" ")
                positions.append(index)
            previous_space = True
            continue
        chars.append(char.translate(_FOLD))
        positions.append(index)
        previous_space = False
    if chars and chars[-1] == " ":
        chars.pop()
        positions.pop()
    return "".join(chars), positions


def locate_quote(text: str, quote: str) -> tuple[int, int] | None:
    """``(start, end)`` of ``quote`` in ``text``, tolerant to whitespace and
    typographic variants only (no paraphrase, no case folding); ``None`` if
    absent."""
    if not quote.strip():
        return None
    start = text.find(quote)
    if start >= 0:
        return start, start + len(quote)
    # Positions map into ``text`` itself, so it is not re-normalized; fetched
    # and stored texts are NFC already. The quote may come in either form.
    folded_text, positions = _normalize_with_map(text)
    folded_quote, _ = _normalize_with_map(unicodedata.normalize("NFC", quote))
    if not folded_quote:
        return None
    found = folded_text.find(folded_quote)
    if found < 0:
        return None
    return positions[found], positions[found + len(folded_quote) - 1] + 1


def source_span(text: str, quote: str) -> str | None:
    """The verbatim source text matching ``quote``, or ``None``."""
    span = locate_quote(text, quote)
    return text[span[0] : span[1]] if span else None
