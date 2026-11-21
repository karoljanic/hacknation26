from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_SEARCH_QUERIES = 4
MAX_QUERY_CHARS = 80
MAX_COUNTRIES = 3


class TextRegion(BaseModel):
    """Approximate bounding box in normalized image coordinates."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    x_min: float = Field(ge=0, le=1)
    y_min: float = Field(ge=0, le=1)
    x_max: float = Field(ge=0, le=1)
    y_max: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_order(self) -> "TextRegion":
        if self.x_max <= self.x_min or self.y_max <= self.y_min:
            raise ValueError("Text region must have positive width and height")
        return self


class InputSegment(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    origin: Literal["text", "question", "article", "ocr", "audio_transcript", "video_frame"]
    source_url: str | None = None
    source_name: str | None = None
    region: TextRegion | None = None
    start_seconds: float | None = Field(default=None, ge=0)
    end_seconds: float | None = Field(default=None, ge=0)

    @field_validator("id", "text")
    @classmethod
    def reject_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Value must not be blank")
        return value

    @model_validator(mode="after")
    def validate_time(self) -> "InputSegment":
        if (
            self.start_seconds is not None
            and self.end_seconds is not None
            and self.end_seconds < self.start_seconds
        ):
            raise ValueError("Segment end time must not precede start time")
        return self


class ClaimReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    segment_id: str = Field(min_length=1)
    quote: str = Field(min_length=1)


class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    kind: Literal["factual_claim", "verification_question", "opinion"]
    references: list[ClaimReference] = Field(min_length=1)
    ambiguities: list[str]
    # Short keyword queries for search engines that match words, not meaning
    # (Google Fact Check, gov.pl search); include synonyms of the claim wording.
    search_queries: list[str] = Field(default_factory=list)
    # ISO 3166-1 alpha-2 codes of foreign countries the claim is about, for the
    # MSZ travel advisories (Odyseusz).
    countries: list[str] = Field(default_factory=list)
    # Established, time-independent fact of history, geography, science or
    # mathematics; only such claims may be answered from general knowledge.
    general_knowledge: bool = False
    # A question asking for information ("Kiedy...?", "Ile...?") rather than
    # whether a statement is true: outside a true/false checker's scope.
    open_question: bool = False

    @field_validator("id", "text")
    @classmethod
    def reject_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Value must not be blank")
        return value

    @field_validator("search_queries")
    @classmethod
    def clean_queries(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for value in values:
            query = " ".join(value.split())
            if query and len(query) <= MAX_QUERY_CHARS and query not in cleaned:
                cleaned.append(query)
        return cleaned[:MAX_SEARCH_QUERIES]

    @field_validator("countries")
    @classmethod
    def clean_countries(cls, values: list[str]) -> list[str]:
        # Invalid codes are dropped, not fatal: they only steer an optional source.
        codes: list[str] = []
        for value in values:
            code = value.strip().upper()
            if len(code) == 2 and code.isascii() and code.isalpha() and code not in codes:
                codes.append(code)
        return codes[:MAX_COUNTRIES]


class ClaimExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claims: list[Claim]
