"""Split normalised document text into citable passages.

Passages follow paragraph boundaries (``\\n``) and are packed up to a target
size; a paragraph longer than the maximum is split on sentence boundaries,
and only a single over-long sentence is split hard. ``char_start``/``char_end``
index into the document text, so ``text[c.char_start:c.char_end] == c.text``.
"""

import re
from dataclasses import dataclass

TARGET_CHARS = 1000
MAX_CHARS = 1500

_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+(?=\S)")


@dataclass(frozen=True)
class Chunk:
    ordinal: int
    text: str
    char_start: int
    char_end: int


def _spans(text: str, pattern: re.Pattern[str], start: int, end: int) -> list[tuple[int, int]]:
    """Split text[start:end] at pattern matches; return non-empty stripped spans."""
    spans: list[tuple[int, int]] = []
    cursor = start
    for match in pattern.finditer(text, start, end):
        spans.append((cursor, match.start()))
        cursor = match.end()
    spans.append((cursor, end))
    result = []
    for s, e in spans:
        while s < e and text[s].isspace():
            s += 1
        while e > s and text[e - 1].isspace():
            e -= 1
        if e > s:
            result.append((s, e))
    return result


def _units(text: str, max_chars: int) -> list[tuple[int, int]]:
    """Paragraphs, with over-long ones broken into sentences / hard pieces."""
    units: list[tuple[int, int]] = []
    for p_start, p_end in _spans(text, re.compile(r"\n+"), 0, len(text)):
        if p_end - p_start <= max_chars:
            units.append((p_start, p_end))
            continue
        for s_start, s_end in _spans(text, _SENTENCE_END, p_start, p_end):
            while s_end - s_start > max_chars:
                cut = text.rfind(" ", s_start, s_start + max_chars)
                cut = cut if cut > s_start else s_start + max_chars
                units.append((s_start, cut))
                s_start = cut
                while s_start < s_end and text[s_start].isspace():
                    s_start += 1
            if s_end > s_start:
                units.append((s_start, s_end))
    return units


def chunk_text(text: str, *, target_chars: int = TARGET_CHARS, max_chars: int = MAX_CHARS) -> list[Chunk]:
    if target_chars <= 0 or max_chars < target_chars:
        raise ValueError("Require 0 < target_chars <= max_chars")
    chunks: list[Chunk] = []
    current: tuple[int, int] | None = None
    for start, end in _units(text, max_chars):
        if current is not None and (
            current[1] - current[0] >= target_chars or end - current[0] > max_chars
        ):
            chunks.append(Chunk(len(chunks), text[current[0] : current[1]], *current))
            current = None
        current = (start, end) if current is None else (current[0], end)
    if current is not None:
        chunks.append(Chunk(len(chunks), text[current[0] : current[1]], *current))
    return chunks
