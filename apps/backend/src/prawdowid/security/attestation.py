"""Bezstanowe podpisywanie i weryfikacja odpowiedzi Prawdowida (HMAC-SHA256, 128 bitów).

Normalizacja przed obliczeniem skrótu ogranicza wrażliwość na błędy OCR i drobne
różnice formatowania (kopiowanie tekstu, zrzuty ekranu):
- przed hashowaniem zawsze usuwane są linie zawierające hash/podpis (u góry, u dołu
  lub w treści), aby sam hash nigdy nie był częścią wejścia funkcji haszującej,
- polskie znaki diakrytyczne -> łacińskie odpowiedniki (w tym ł -> l),
- pozostałe znaki ze znakami diakrytycznymi -> odpowiedniki ASCII (NFKD),
- zamiana na małe litery,
- każdy ciąg białych znaków lub znaków niealfanumerycznych -> pojedynczy średnik `;`.

Podpis to 128-bitowy prefiks HMAC-SHA256 (32 znaki szesnastkowe).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
import hashlib
import hmac
import os
import re
import unicodedata

from prawdowid.domain.enums import Strength, Verdict

SIGNING_KEY_ENV = "PRAWDOWID_SIGNING_KEY"
HASH_HEX_LENGTH = 32  # 128 bitów (16 bajtów) w zapisie szesnastkowym

VERDICT_LABEL: dict[Verdict, str] = {
    Verdict.CONFIRMED: "Potwierdzone",
    Verdict.FALSE: "Fałszywe",
    Verdict.MISLEADING: "Wprowadzające w błąd",
    Verdict.INSUFFICIENT_EVIDENCE: "Brak wystarczających dowodów",
    Verdict.UNVERIFIABLE: "Nie podlega weryfikacji",
}

STRENGTH_LABEL: dict[Strength, str] = {
    Strength.STRONG: "Silna podstawa",
    Strength.MODERATE: "Umiarkowana podstawa",
    Strength.WEAK: "Słaba podstawa",
}

_POLISH_TRANSLATION = str.maketrans(
    "ąćęłńóśźżĄĆĘŁŃÓŚŹŻ",
    "acelnoszzACELNOSZZ",
)
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_HEX_RE = re.compile(rf"^[0-9a-f]{{{HASH_HEX_LENGTH}}}$")
# Dopasowuje dokładnie 32 znaki szesnastkowe (128 bitów), nawet jeśli są zawinięte
# w nowej linii albo rozdzielone spacjami/myślnikami — nie „zjada” kolejnego wiersza.
_HEX_FLEX = rf"(?:[0-9a-fA-F][\s-]*){{{HASH_HEX_LENGTH - 1}}}[0-9a-fA-F]"

_HASH_PREFIX_RE = re.compile(
    r"^(?:hmac(?:[-_ ]?sha256)?|sha256|hash|podpis(?:\s+weryfikacyjny)?|kod(?:\s+weryfikacyjny)?)\s*[:=]\s*",
    re.IGNORECASE,
)
_LABELED_HASH_LINE_RE = re.compile(
    rf"(?:^|\n)[ \t]*(?:podpis[ \t]+weryfikacyjny[ \t]+prawdowida[ \t]*\n)?"
    rf"[ \t]*(?:hash|podpis(?:[ \t]+weryfikacyjny)?|kod(?:[ \t]+weryfikacyjny)?|hmac(?:[-_ ]?sha256)?)"
    rf"[ \t]*[:=][ \t]*({_HEX_FLEX})(?![0-9a-fA-F])[ \t]*(?=\n|$)",
    re.IGNORECASE,
)
_STANDALONE_HASH_LINE_RE = re.compile(
    rf"(?:^|\n)[ \t]*({_HEX_FLEX})(?![0-9a-fA-F])[ \t]*(?=\n|$)"
)
_INLINE_HASH_RE = re.compile(
    rf"(?<![0-9a-zA-Z])([0-9a-fA-F]{{{HASH_HEX_LENGTH}}})(?![0-9a-zA-Z])"
)


@dataclass(frozen=True)
class SignableClaim:
    """Minimalny zestaw widocznych pól twierdzenia objętych podpisem."""

    text: str
    verdict: Verdict | str
    explanation: str
    strength: Strength | str | None = None
    limitations: str | None = None
    missing_details: Sequence[str] = field(default_factory=tuple)


def get_signing_key() -> str | None:
    """Zwraca klucz prywatny z env ``PRAWDOWID_SIGNING_KEY`` albo ``None``."""
    value = os.environ.get(SIGNING_KEY_ENV, "").strip()
    return value or None


def normalize_for_hash(text: str) -> str:
    """Normalizuje tekst przed obliczeniem HMAC-SHA256 (odporność na OCR).

    Reguły:
    1. Polskie znaki (ą, ć, ę, ł, ń, ó, ś, ź, ż) -> odpowiedniki łacińskie.
    2. Pozostałe znaki diakrytyczne -> usunięcie akcentów przez NFKD.
    3. Małe litery.
    4. Białe znaki oraz dowolne ciągi znaków niealfanumerycznych -> jeden średnik `;`.
    5. Usunięcie skrajnych średników.
    """
    translated = text.translate(_POLISH_TRANSLATION)
    decomposed = unicodedata.normalize("NFKD", translated)
    ascii_only = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    lowered = ascii_only.lower()
    return _NON_ALNUM_RE.sub(";", lowered).strip(";")


def _verdict_label(verdict: Verdict | str) -> str:
    if isinstance(verdict, Verdict):
        return VERDICT_LABEL[verdict]
    try:
        return VERDICT_LABEL[Verdict(verdict.strip().lower())]
    except ValueError:
        return verdict.strip()


def _strength_label(strength: Strength | str | None) -> str | None:
    if strength is None:
        return None
    if isinstance(strength, Strength):
        return STRENGTH_LABEL[strength]
    cleaned = strength.strip()
    if not cleaned:
        return None
    try:
        return STRENGTH_LABEL[Strength(cleaned.lower())]
    except ValueError:
        return cleaned


def format_answer_text(
    claims: Sequence[SignableClaim],
    *,
    fallback_message: str | None = None,
) -> str:
    """Buduje deterministyczną, czytelną treść odpowiedzi (bez linii z hashem)."""
    blocks: list[str] = []
    for claim in claims:
        lines: list[str] = []
        if claim.text.strip():
            lines.append(claim.text.strip())
        v_label = _verdict_label(claim.verdict)
        s_label = _strength_label(claim.strength)
        if v_label and s_label:
            lines.append(f"{v_label} ({s_label})")
        elif v_label:
            lines.append(v_label)
        if claim.explanation.strip():
            lines.append(claim.explanation.strip())
        for detail in claim.missing_details:
            if detail and detail.strip():
                lines.append(detail.strip())
        if claim.limitations and claim.limitations.strip():
            lines.append(claim.limitations.strip())
        if lines:
            blocks.append("\n".join(lines))

    if blocks:
        return "\n\n".join(blocks)
    if fallback_message and fallback_message.strip():
        return fallback_message.strip()
    return ""


def clean_hash(value: str | None) -> str | None:
    """Czyści hash z prefiksów, białych znaków i myślników; zwraca 32-znakowy hex (128 bitów) lub ``None``."""
    if not value:
        return None
    stripped = _HASH_PREFIX_RE.sub("", value.strip())
    compact = re.sub(r"[\s-]+", "", stripped).lower()
    return compact if _HEX_RE.fullmatch(compact) else None


def extract_hash_and_body(raw_text: str) -> tuple[str, str | None]:
    """Rozdziela wklejoną odpowiedź na treść właściwą (bez hasha) i wyodrębniony hash.

    Obsługuje hash z etykietą (`Hash: ...`), w osobnej linii oraz inline obok
    etykiety werdyktu (np. „Potwierdzone <hash> Silna podstawa”).
    """
    text = raw_text.strip()
    if not text:
        return "", None

    found_hash: str | None = None
    body = text

    for pattern, replacement in (
        (_LABELED_HASH_LINE_RE, "\n"),
        (_STANDALONE_HASH_LINE_RE, "\n"),
        (_INLINE_HASH_RE, " "),
    ):
        def _replace(match: re.Match[str], repl: str = replacement) -> str:
            nonlocal found_hash
            candidate = clean_hash(match.group(1))
            if candidate is None:
                return match.group(0)
            if found_hash is None:
                found_hash = candidate
            return repl

        body = pattern.sub(_replace, body)

    return body.strip(), found_hash


def strip_hash_lines(text: str) -> str:
    """Usuwa z tekstu wszelkie linie z hashem weryfikacyjnym, aby nie trafiły do funkcji haszującej."""
    body, _ = extract_hash_and_body(text)
    return body


def compute_answer_hash(text: str, *, secret_key: str) -> str:
    """Liczy 128-bitowy podpis (32 znaki hex) z HMAC-SHA256 klucza prywatnego i znormalizowanej treści (bez hasha)."""
    if not secret_key or not secret_key.strip():
        raise ValueError("Klucz podpisujący (PRAWDOWID_SIGNING_KEY) nie może być pusty.")
    body_without_hash = strip_hash_lines(text)
    normalized = normalize_for_hash(body_without_hash)
    if not normalized:
        raise ValueError("Treść odpowiedzi po normalizacji jest pusta.")
    digest = hmac.new(
        secret_key.encode("utf-8"),
        normalized.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return digest[:HASH_HEX_LENGTH]


def verify_answer_hash(text: str, provided_hash: str, *, secret_key: str) -> bool:
    """Sprawdza w stałym czasie, czy 128-bitowy hash odpowiada znormalizowanej treści (bez linii hasha)."""
    cleaned = clean_hash(provided_hash)
    if cleaned is None:
        return False
    body_without_hash = strip_hash_lines(text)
    normalized = normalize_for_hash(body_without_hash)
    if not normalized:
        return False
    expected = compute_answer_hash(body_without_hash, secret_key=secret_key)
    return hmac.compare_digest(expected, cleaned)