from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from prawdowid.domain.claims import InputSegment, TextRegion


class MediaFragment(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    text: str = Field(min_length=1)
    origin: Literal["ocr", "audio_transcript", "video_frame"]
    start_seconds: float | None
    end_seconds: float | None
    region: TextRegion | None


class MediaReading(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["extracted", "no_content", "unreadable"]
    reason: str
    fragments: list[MediaFragment]


class PreparedInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    segments: list[InputSegment] = Field(min_length=1)
    title: str | None = None
    fetched_at: datetime | None = None
    preparation_model: str | None = None
    preparation_model_version: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    notes: list[str] = Field(default_factory=list)
