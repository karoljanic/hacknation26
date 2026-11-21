"""embed_pending against real Postgres, inside a rolled-back transaction."""

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from prawdowid.ai.adapters.embedding import EmbeddingResult
from prawdowid.domain.enums import SourceKind
from prawdowid.persistence.models import EMBEDDING_DIM, Document, Passage, Source
from prawdowid.services.embedding import embed_pending

SLUG = "test-embed"


class FakeEmbedder:
    def __init__(self, fail_after: int | None = None, dims: int = EMBEDDING_DIM) -> None:
        self.calls = 0
        self.fail_after = fail_after
        self.dims = dims

    def embed(self, content: str) -> EmbeddingResult:
        if self.fail_after is not None and self.calls >= self.fail_after:
            raise RuntimeError("429 quota exceeded")
        self.calls += 1
        return EmbeddingResult([1.0] + [0.0] * (self.dims - 1), "fake-embedding")


@pytest.fixture
def passages(isolated_sessions):
    now = datetime.now(timezone.utc)
    with isolated_sessions() as session, session.begin():
        source = Source(slug=SLUG, name="T", publisher="T", kind=SourceKind.OFFICIAL, domain="t.pl",
                        tier=1, license="t", inclusion_rule="t")
        session.add(source)
        session.flush()
        document = Document(
            source_id=source.id, url="https://t.pl/a", title="T", fetched_at=now, content_hash="h", lang="pl",
            passages=[Passage(ordinal=i, text=f"Fragment {i}", char_start=i * 20, char_end=i * 20 + 10)
                      for i in range(5)],
        )
        session.add(document)
    return isolated_sessions


def embedded(sessions) -> list[str | None]:
    with sessions() as session:
        return list(session.scalars(
            select(Passage.embedding_model)
            .join(Document).join(Source).where(Source.slug == SLUG).order_by(Passage.ordinal)
        ))


def test_fills_only_missing_vectors_in_batches(passages) -> None:
    embedder = FakeEmbedder()
    result = embed_pending(passages, embedder, limit=3, batch_size=2, source_slug=SLUG)
    assert result.embedded == 3 and result.remaining == 2 and not result.errors
    assert sum(m == "fake-embedding" for m in embedded(passages)) == 3

    again = embed_pending(passages, embedder, limit=100, source_slug=SLUG)
    assert again.embedded == 2 and again.remaining == 0
    assert embedder.calls == 5  # nothing embedded twice
    assert embed_pending(passages, embedder, source_slug=SLUG).embedded == 0


def test_provider_failure_keeps_progress_and_leaves_rest_null(passages) -> None:
    result = embed_pending(passages, FakeEmbedder(fail_after=2), limit=100, batch_size=10, source_slug=SLUG)
    assert result.embedded == 2 and result.remaining == 3
    assert "429" in result.errors[0]
    assert embedded(passages).count(None) == 3


def test_wrong_dimension_is_rejected(passages) -> None:
    result = embed_pending(passages, FakeEmbedder(dims=3), limit=100, source_slug=SLUG)
    assert result.embedded == 0 and "dims" in result.errors[0]
    assert embedded(passages).count(None) == 5


def test_source_filter_is_passage_only(passages) -> None:
    with pytest.raises(ValueError):
        embed_pending(passages, FakeEmbedder(), target="claim", source_slug=SLUG)
