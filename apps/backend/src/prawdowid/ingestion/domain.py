from collections.abc import Iterator, Mapping
from datetime import datetime
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator


class CorpusDocument(BaseModel):
    """What every collector yields, independent of the source it came from."""

    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1)
    title: str = Field(min_length=1)
    text: str
    fetched_at: datetime
    published_at: datetime | None = None
    lang: str = "pl"
    # Site of the publisher for aggregated feeds (fact-check reviews): the
    # service checks it against the catalogued domain of the source.
    publisher_site: str | None = None
    # Source-specific facts kept verbatim (rating, claimant, keywords, ...).
    metadata: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("fetched_at", "published_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("Timestamps must be timezone-aware")
        return value


class Skipped(BaseModel):
    """Yielded instead of a document for one unusable item; the run continues.

    (Raising inside a generator would end it, so skips are values, not exceptions.)
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    reason: str
    ref: str | None = None  # URL / record id, for the run's error log


class CollectorUnavailable(RuntimeError):
    """The collector cannot run at all (e.g. missing API key): the run is skipped."""


class Collector(Protocol):
    def collect(self, params: Mapping[str, Any]) -> Iterator[CorpusDocument | Skipped]:
        """Yield documents or ``Skipped`` markers. Any exception aborts the run
        (recorded as ``failed``, documents stored before it are kept)."""
        ...
