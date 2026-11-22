"""Locating a model-reported quote in its source text.

Models reproduce quotes faithfully but not byte-for-byte: Polish typography
puts non-breaking spaces after one-letter words ("Z\\xa0takiej", "i\\xa0seniorki"),
pages use typographic quotes and dashes, and line breaks become spaces. An
exact ``quote in text`` check rejects such quotes, so matching ignores only
these presentation differences and then returns the *source's own* span,
which is what gets shown and stored: the quote stays verbatim.
"""

import re
import unicodedata

_QUOTES = {ord(c): '"' for c in "„”“‟«»″"} | {ord(c): "'" for c in "’‘‚‛′"}
_DASHES = {ord(c): "-" for c in "‐‑‒–—―−"}
_FOLD = _QUOTES | _DASHES
_ELISION = re.compile(r"\s*(?:\[\s*(?:\.\.\.|…)\s*\]|\(\s*(?:\.\.\.|…)\s*\)|\.\.\.|…)\s*")
MAX_ELIDED_CHARS = 300


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
    absent.

    A quote shortened with an elision ("A [...] B") matches when its parts
    occur in order and close together; the span then covers the omitted
    middle too, so what is stored is still the source's own contiguous text.
    """
    if not quote.strip():
        return None
    parts = [part for part in (p.strip() for p in _ELISION.split(quote)) if part]
    if len(parts) > 1:
        return _locate_elided(text, parts, len(quote))
    return _locate_whole(text, quote)


def _locate_elided(text: str, parts: list[str], quote_length: int) -> tuple[int, int] | None:
    spans: list[tuple[int, int]] = []
    offset = 0
    for part in parts:
        span = _locate_whole(text[offset:], part)
        if span is None:
            return None
        spans.append((offset + span[0], offset + span[1]))
        offset += span[1]
    start, end = spans[0][0], spans[-1][1]
    # An elision may skip a clause, not half the page.
    if end - start > quote_length + MAX_ELIDED_CHARS:
        return None
    return start, end


def _locate_whole(text: str, quote: str) -> tuple[int, int] | None:
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
