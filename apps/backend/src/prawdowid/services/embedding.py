"""Fill in missing embeddings for passages and claims.

Independent of how rows were created (corpus ingestion, user claims): it only
looks for ``embedding IS NULL``. Rows are claimed with ``FOR UPDATE SKIP
LOCKED`` so several workers can run at once without embedding a row twice.
A provider error ends the batch; untouched rows simply stay NULL for the
next run.
"""

import logging
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from prawdowid.ai.adapters.embedding import EmbeddingAdapter
from prawdowid.persistence.models import EMBEDDING_DIM, Claim, Document, Passage, Source

log = logging.getLogger(__name__)

Target = Literal["passage", "claim"]
_MODELS = {"passage": (Passage, "text"), "claim": (Claim, "canonical_text")}


class EmbedderUnavailable(RuntimeError):
    pass


@contextmanager
def default_embedder() -> Iterator[EmbeddingAdapter]:
    """Gemini embeddings truncated to the DB vector size (needs GEMINI_API_KEY)."""
    from google import genai
    from google.genai import types

    from prawdowid.ai.instances.gemini_embeddings import GeminiEmbeddingAdapter

    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        raise EmbedderUnavailable("GEMINI_API_KEY is not set")
    with genai.Client(api_key=key, http_options=types.HttpOptions(timeout=30000)) as client:
        yield GeminiEmbeddingAdapter(client, dimensions=EMBEDDING_DIM)


@dataclass
class EmbeddingRunResult:
    target: Target
    embedded: int = 0
    remaining: int | None = None
    errors: list[str] = field(default_factory=list)


def embed_pending(
    session_factory: Callable[[], Session],
    embedder: EmbeddingAdapter,
    *,
    target: Target = "passage",
    limit: int = 200,
    batch_size: int = 20,
    source_slug: str | None = None,
) -> EmbeddingRunResult:
    """``source_slug`` restricts passage embedding to one catalogued source."""
    model, text_attr = _MODELS[target]
    if source_slug is not None and target != "passage":
        raise ValueError("source_slug applies to passages only")
    pending = select(model).where(model.embedding.is_(None))
    if source_slug is not None:
        pending = (
            pending.join(Document, Document.id == Passage.document_id)
            .join(Source, Source.id == Document.source_id)
            .where(Source.slug == source_slug)
        )
    result = EmbeddingRunResult(target)
    while result.embedded < limit:
        size = min(batch_size, limit - result.embedded)
        with session_factory() as session, session.begin():
            rows = session.scalars(
                pending.order_by(model.id).limit(size).with_for_update(skip_locked=True, of=model)
            ).all()
            if not rows:
                break
            try:
                for row in rows:
                    vector = embedder.embed(getattr(row, text_attr))
                    if len(vector.values) != EMBEDDING_DIM:
                        raise RuntimeError(
                            f"embedding has {len(vector.values)} dims, expected {EMBEDDING_DIM}"
                        )
                    row.embedding = vector.values
                    row.embedding_model = vector.model
                    result.embedded += 1
            except Exception as exc:
                # Keep the vectors computed so far in this batch; stop here.
                log.exception("Embedding %s failed", target)
                result.errors.append(f"{type(exc).__name__}: {exc}")
                break
    with session_factory() as session:
        result.remaining = session.scalar(
            select(func.count()).select_from(pending.with_only_columns(model.id).subquery())
        )
    return result
