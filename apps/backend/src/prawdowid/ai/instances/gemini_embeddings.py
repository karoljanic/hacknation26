import math

from google import genai
from google.genai import types

from prawdowid.ai.adapters.embedding import EmbeddingResult
from prawdowid.ai.adapters.media import Media


class GeminiEmbeddingAdapter:
    def __init__(self, client: genai.Client, model: str = "gemini-embedding-2") -> None:
        self.client = client
        self.model = model

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
        response = self.client.models.embed_content(model=self.model, contents=contents)
        if not response.embeddings or len(response.embeddings) != 1:
            raise RuntimeError("Embedding provider did not return exactly one vector")
        values = response.embeddings[0].values
        if not values or not all(math.isfinite(value) for value in values):
            raise RuntimeError("Embedding provider returned an empty or invalid vector")
        return EmbeddingResult(values=list(values), model=self.model)
