"""Deterministyczne wykrywanie prób prompt injection w wejściu użytkownika, ukrytym HTML i źródłach.

Wykryte próby są wyłącznie rejestrowane do audytu (`verification_run.injection_flags`)
i nigdy nie zmieniają werdyktu ani nie przerywają weryfikacji.
"""

from __future__ import annotations

import re
from typing import Any
import unicodedata

from bs4 import BeautifulSoup, Comment, Tag

from prawdowid.adapters.sources.gov_pl import _is_hidden_tag

_POLISH_TRANSLATION = str.maketrans(
    "ąćęłńóśźżĄĆĘŁŃÓŚŹŻ",
    "acelnoszzACELNOSZZ",
)
_SKIP_RAW_HTML_TAGS = frozenset({"script", "style", "noscript", "svg", "template"})

# Wzorce dopasowywane do tekstu bez polskich znaków diakrytycznych (1-do-1 z oryginałem).
_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Ignorowanie / nadpisywanie instrukcji, zasad lub wszystkiego powyżej (PL)
    re.compile(
        r"\b(?:zignoruj|ignoruj|pomin|zapomnij(?:\s+o)?|olej|odrzuc|nadpisz|uchyl|wylacz)\b"
        r"(?:\s+\w+){0,4}\s+"
        r"(?:instrukcj\w*|zasad\w*|regul\w*|polece[n]\w*|wytyczn\w*|prompt\w*|"
        r"ogranicze[n]\w*|zabezpiecze[n]\w*|wszystk\w*|powyzsz\w*|wczesniejsz\w*|poprzedni\w*)",
        re.IGNORECASE,
    ),
    # Ignorowanie / nadpisywanie instrukcji (EN)
    re.compile(
        r"\b(?:ignore|disregard|forget|override|bypass|skip)\b"
        r"(?:\s+\w+){0,4}\s+"
        r"(?:instructions?|rules?|prompts?|guidelines?|directions?|constraints?|"
        r"everything|above|previous|prior|safety|guardrails?)",
        re.IGNORECASE,
    ),
    # Wymuszanie werdyktu / oceny / odpowiedzi (PL)
    re.compile(
        r"\b(?:ustaw|zwroc|wydaj|oznacz|zmien|uznaj|potwierdz|napisz|odpowiedz|powiedz|zadeklaruj)\b"
        r".{0,45}\b(?:werdykt|ocen[ea]|status|wynik|twierdzenie|to|ze)\b"
        r".{0,35}\b(?:potwierdzon\w*|falszyw\w*|prawdziw\w*|prawd[ea]|falsz\w*|confirmed|false|true)\b",
        re.IGNORECASE,
    ),
    # Bezpośrednie wymuszenie etykiety / pominięcie źródeł (PL)
    re.compile(
        r"\b(?:oznacz\s+(?:to\s+)?jako\s+(?:potwierdzon\w*|prawdziw\w*|falszyw\w*)|"
        r"nie\s+sprawdzaj\s+(?:zrodel|faktow)|bez\s+sprawdzania\s+zrodel|"
        r"pomin\s+weryfikacj\w*|nie\s+korzystaj\s+ze\s+zrodel|"
        r"niewazne\s+co\s+mowia\s+zrodla|zawsze\s+odpowiadaj)\b",
        re.IGNORECASE,
    ),
    # Wymuszanie werdyktu / odpowiedzi (EN)
    re.compile(
        r"\b(?:set|return|output|mark|classify|force|change|treat|respond|say|write)\b"
        r".{0,40}\b(?:verdict|status|claim|answer|this|it)\b"
        r".{0,35}\b(?:confirmed|false|true|misleading|factual)\b",
        re.IGNORECASE,
    ),
    # Przejęcie roli / tryb developerski / jailbreak (PL & EN)
    re.compile(
        r"\b(?:od\s+teraz\s+jestes|jestes\s+teraz|zachowuj\s+sie\s+jak|udawaj\s+ze|"
        r"wciel\s+sie\s+w|nowe\s+instrukcje|przejdz\s+w\s+tryb|tryb\s+deweloperski|"
        r"you\s+are\s+now|from\s+now\s+on|act\s+as|pretend\s+to\s+be|"
        r"new\s+instructions?|developer\s+mode|jailbreak)\b",
        re.IGNORECASE,
    ),
    # Próba wycieku promptu systemowego lub sekretów (PL & EN)
    re.compile(
        r"\b(?:pokaz|ujawnij|wypisz|podaj|wyswietl|zdradz|reveal|show|print|leak|dump)\b"
        r".{0,40}\b(?:prompt\w*|instrukcj\w*\s+systemow\w*|swoje\s+instrukcj\w*|"
        r"swoje\s+zasad\w*|system\s+prompt|system\s+instructions?|hidden\s+instructions?|"
        r"api\s+key|klucz\w*\s+api)\b",
        re.IGNORECASE,
    ),
    # Podszywanie się pod role / znaczniki systemowe
    re.compile(
        r"(?:^|\n|\s)(?:###\s*)?(?:system|developer|assistant)\s*(?:prompt|instruction|message|override)?\s*:"
        r"|\[(?:system|inst|override)\]"
        r"|<<\s*sys\s*>>"
        r"|<\|(?:im_start|im_end|system|endoftext)\|>",
        re.IGNORECASE,
    ),
)

_MAX_EXCERPT_CHARS = 260


def _fold_char(ch: str) -> str:
    """Zamienia pojedynczy znak na 1 znak ASCII, zachowując mapowanie indeksów 1-do-1."""
    mapped = ch.translate(_POLISH_TRANSLATION)
    if mapped.isascii():
        return mapped
    decomposed = unicodedata.normalize("NFKD", mapped)
    base = "".join(c for c in decomposed if not unicodedata.combining(c))
    return base[0] if base else " "


def _fold_for_matching(text: str) -> str:
    return "".join(_fold_char(ch) for ch in text)


def _excerpt_around(text: str, start: int, end: int) -> str:
    lo = max(0, start - 60)
    hi = min(len(text), end + 120)
    snippet = " ".join(text[lo:hi].split()).strip()
    return snippet[:_MAX_EXCERPT_CHARS]


def detect_prompt_injections(
    text: str,
    *,
    where: str,
    claim_id: str | None = None,
    detected_by: str = "heuristic",
) -> list[dict[str, Any]]:
    """Zwraca listę wykrytych prób prompt injection w podanym tekście."""
    if not text or not text.strip():
        return []

    folded = _fold_for_matching(text)
    flags: list[dict[str, Any]] = []
    seen_excerpts: set[str] = set()
    for pattern in _PATTERNS:
        for match in pattern.finditer(folded):
            excerpt = _excerpt_around(text, match.start(), match.end())
            key = excerpt.casefold()
            if not excerpt or key in seen_excerpts:
                continue
            seen_excerpts.add(key)
            flags.append(
                {
                    "where": where,
                    "claim_id": claim_id,
                    "excerpt": excerpt,
                    "detected_by": detected_by,
                }
            )
            if len(flags) >= 5:
                return flags
    return flags


def detect_hidden_html_injections(
    html: bytes | str,
    *,
    encoding: str | None = None,
) -> list[str]:
    """Wykrywa próby prompt injection ukryte w komentarzach HTML lub elementach ukrytych w CSS."""
    soup = (
        BeautifulSoup(html, "html.parser", from_encoding=encoding)
        if isinstance(html, bytes)
        else BeautifulSoup(html, "html.parser")
    )

    hidden_texts: list[str] = []
    for comment in soup.find_all(string=lambda node: isinstance(node, Comment)):
        raw = str(comment).strip()
        if raw:
            hidden_texts.append(raw)

    for node in soup.find_all(True):
        if not isinstance(node, Tag) or node.name in _SKIP_RAW_HTML_TAGS:
            continue
        if _is_hidden_tag(node):
            raw = node.get_text(" ", strip=True)
            if raw:
                hidden_texts.append(raw)

    excerpts: list[str] = []
    seen: set[str] = set()
    for block in hidden_texts:
        for flag in detect_prompt_injections(block, where="hidden_html", detected_by="hidden_html_heuristic"):
            excerpt = str(flag["excerpt"]).strip()
            key = excerpt.casefold()
            if excerpt and key not in seen:
                seen.add(key)
                excerpts.append(excerpt)
                if len(excerpts) >= 5:
                    return excerpts
    return excerpts


def dedupe_injection_flags(flags: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Usuwa duplikaty flag zachowując kolejność."""
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in flags:
        if not isinstance(item, dict):
            continue
        where = str(item.get("where") or "")
        claim_id = str(item.get("claim_id") or "")
        excerpt = str(item.get("excerpt") or item.get("pattern") or "").strip()
        key = (where, claim_id, excerpt.casefold())
        if not excerpt or key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result