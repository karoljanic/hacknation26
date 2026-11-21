"""Claim lookup / creation used to deduplicate claims across submissions."""

import hashlib
import re
import unicodedata
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from prawdowid.domain.enums import ClaimKind, ClaimOrigin
from prawdowid.persistence.models import EMBEDDING_DIM, Claim

_WHITESPACE = re.compile(r"\s+")


def normalize_claim_text(text: str) -> str:
    """Case/whitespace/trailing-punctuation insensitive form used for exact matching."""
    text = unicodedata.normalize("NFC", text).casefold()
    return _WHITESPACE.sub(" ", text).strip().rstrip(".!?…").strip()


_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
_WORD = re.compile(r"\w+", re.UNICODE)
_NEGATIONS = frozenset({"nie", "nigdy", "bez", "brak", "żaden", "żadna", "żadne", "żadnego", "ani"})


def same_specifics(first: str, second: str) -> bool:
    """True when two claim texts agree on every number and on negation.

    Embeddings place "VAT wynosi 23%" next to "VAT wynosi 25%" and a claim next
    to its negation, so vector similarity alone must never merge such claims:
    the verdict for one is not the verdict for the other.
    """

    def numbers(text: str) -> list[str]:
        return sorted(match.replace(",", ".") for match in _NUMBER.findall(text))

    def negated(text: str) -> bool:
        # Polish negative concord ("nigdy nie") is one negation, so presence
        # matters, not the count.
        return any(word in _NEGATIONS for word in _WORD.findall(text.casefold()))

    return numbers(first) == numbers(second) and negated(first) == negated(second)


def claim_text_hash(text: str) -> str:
    return hashlib.sha256(normalize_claim_text(text).encode()).hexdigest()


def find_exact(session: Session, text: str, kind: ClaimKind) -> Claim | None:
    return session.scalars(
        select(Claim)
        .where(Claim.text_hash == claim_text_hash(text), Claim.kind == kind)
        .order_by(Claim.created_at)
        .limit(1)
    ).first()


def find_similar(
    session: Session,
    embedding: Sequence[float],
    *,
    kind: ClaimKind,
    embedding_model: str,
    max_distance: float,
) -> tuple[Claim, float] | None:
    """Nearest claim of the same kind by cosine distance, if within ``max_distance``.

    Only vectors from the same embedding model are comparable.
    """
    distance = Claim.embedding.cosine_distance(list(embedding))
    row = session.execute(
        select(Claim, distance)
        .where(
            Claim.kind == kind,
            Claim.embedding_model == embedding_model,
            Claim.embedding.is_not(None),
        )
        .order_by(distance)
        .limit(1)
    ).first()
    if row is None or row[1] > max_distance:
        return None
    return row[0], float(row[1])


def create_claim(
    session: Session,
    text: str,
    *,
    kind: ClaimKind,
    origin: ClaimOrigin,
    embedding: Sequence[float] | None = None,
    embedding_model: str | None = None,
    lang: str = "pl",
) -> Claim:
    if embedding is not None and len(embedding) != EMBEDDING_DIM:
        raise ValueError(f"Claim embedding must have {EMBEDDING_DIM} dimensions")
    claim = Claim(
        canonical_text=text.strip(),
        text_hash=claim_text_hash(text),
        lang=lang,
        kind=kind,
        origin=origin,
        embedding=list(embedding) if embedding is not None else None,
        embedding_model=embedding_model if embedding is not None else None,
    )
    session.add(claim)
    session.flush()
    return claim
