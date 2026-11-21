from dataclasses import dataclass
from typing import Protocol

from prawdowid.ai.adapters.media import Media


@dataclass(frozen=True)
class EmbeddingResult:
    values: list[float]
    model: str


class EmbeddingAdapter(Protocol):
    def embed(self, content: str | Media) -> EmbeddingResult: ...
