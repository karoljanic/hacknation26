import math

from google import genai
from google.genai import types

from prawdowid.ai.adapters.embedding import EmbeddingResult
from prawdowid.ai.adapters.media import Media


class GeminiEmbeddingAdapter:
    def __init__(
        self,
        client: genai.Client,
        model: str = "gemini-embedding-2",
        *,
        dimensions: int | None = None,
    ) -> None:
        """``dimensions`` truncates the vector (Matryoshka) to fit a fixed-size index."""
        if dimensions is not None and dimensions <= 0:
            raise ValueError("Embedding dimensions must be positive")
        self.client = client
        self.model = model
        self.dimensions = dimensions

    def embed(self, content: str | Media) -> EmbeddingResult:
        if isinstance(content, str):
            if not content.strip():
                raise ValueError("Text must not be empty")
            contents = types.Content(parts=[types.Part.from_text(text=content)])
        else:
            if not content.data or not content.mime_type.strip():
                raise ValueError("Media must contain data and a MIME type")
            contents = types.Content(
                parts=[types.Part.from_bytes(data=content.data, mime_type=content.mime_type)]
            )
        if self.dimensions is None:
            response = self.client.models.embed_content(model=self.model, contents=contents)
        else:
            response = self.client.models.embed_content(
                model=self.model,
                contents=contents,
                config=types.EmbedContentConfig(output_dimensionality=self.dimensions),
            )
        if not response.embeddings or len(response.embeddings) != 1:
            raise RuntimeError("Embedding provider did not return exactly one vector")
        values = response.embeddings[0].values
        if not values or not all(math.isfinite(value) for value in values):
            raise RuntimeError("Embedding provider returned an empty or invalid vector")
        if self.dimensions is not None and len(values) != self.dimensions:
            raise RuntimeError("Embedding provider returned a vector of unexpected size")
        return EmbeddingResult(values=list(values), model=self.model)
