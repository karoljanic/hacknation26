from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar

from pydantic import BaseModel, JsonValue

from prawdowid.ai.adapters.media import Media

ResultT = TypeVar("ResultT", bound=BaseModel)


@dataclass(frozen=True)
class GenerationResult:
    text: str
    requested_model: str
    model_version: str | None
    input_tokens: int | None
    output_tokens: int | None
    grounding: dict[str, JsonValue] | None


@dataclass(frozen=True)
class StructuredResult(Generic[ResultT]):
    data: ResultT
    requested_model: str
    model_version: str | None
    input_tokens: int | None
    output_tokens: int | None


class LLMAdapter(Protocol):
    def generate(self, prompt: str, *, search: bool = False) -> GenerationResult: ...

    def generate_structured(
        self,
        prompt: str,
        schema: type[ResultT],
        *,
        instructions: str,
        media: Media | None = None,
    ) -> StructuredResult[ResultT]: ...
