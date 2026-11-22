"""Lexical relevance of texts to a claim: topic words, crude Polish stems and
the passages of a long page that best match a claim.

Shared by evidence search (query building, hit filtering) and the official
source connectors (which must hand the model the part of a 40 000-character
listing that is about the claim, not its first lines).
"""

import re
from collections.abc import Iterable

# Question words, auxiliaries and fact-checker meta words carry no topic.
STOPWORDS = frozenset(
    "czy jest był była było byli są nie się że który która które którym którzy jak dla oraz albo lub "
    "tylko jeszcze przez przed można mają miał miała mieli ma w z na do od po za ze to ten ta tym tej "
    "tego dlaczego czemu gdzie kiedy ile jaki jaka jakie demagog demagoga sprawdzał sprawdzali "
    "sprawdzało analizie analiza".split()
)
WORD = re.compile(r"[\wąćęłńóśźżĄĆĘŁŃÓŚŹŻ%-]+")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+|\n+")

PASSAGE_CHARS = 450
MAX_PASSAGES = 2
PASSAGE_SEPARATOR = " … "


def keywords(text: str) -> list[str]:
    return [
        word
        for word in WORD.findall(text)
        if word.casefold() not in STOPWORDS and (len(word) > 3 or any(ch.isdigit() for ch in word))
    ]


def terms(text: str) -> set[str]:
    # A 5-letter prefix is a crude stem that survives most Polish inflection.
    return {word.casefold()[:5] for word in keywords(text)}


def claim_terms(texts: Iterable[str]) -> set[str]:
    """Terms of a claim together with its search queries (which add synonyms)."""
    found: set[str] = set()
    for text in texts:
        found |= terms(text)
    return found


def score(text: str, wanted: set[str]) -> int:
    return len(wanted & terms(text))


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in _SENTENCE_END.split(text) if part.strip()]


def best_passages(text: str, wanted: set[str]) -> str | None:
    """Up to ``MAX_PASSAGES`` passages of ``text`` about the claim, in reading
    order; ``None`` when no sentence shares any term with it.

    Each passage starts at one of the best-matching sentences (or list lines)
    and continues with the following ones up to ``PASSAGE_CHARS``. Passages
    are verbatim source text, so quotes taken from them stay checkable.
    """
    sentences = _sentences(text)
    scored = sorted(
        ((score(sentence, wanted), index) for index, sentence in enumerate(sentences)),
        key=lambda item: (-item[0], item[1]),
    )
    windows: list[tuple[int, int]] = []
    for points, index in scored:
        if points == 0 or len(windows) == MAX_PASSAGES:
            break
        if any(start <= index < end for start, end in windows):
            continue
        end, length = index + 1, len(sentences[index])
        while end < len(sentences) and length + 1 + len(sentences[end]) <= PASSAGE_CHARS:
            length += 1 + len(sentences[end])
            end += 1
        windows.append((index, end))
    if not windows:
        return None
    return PASSAGE_SEPARATOR.join(" ".join(sentences[start:end]) for start, end in sorted(windows))
