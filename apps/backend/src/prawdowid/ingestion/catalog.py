"""The auditable source catalogue (``data/sources/catalog.toml``) and its DB sync."""

import os
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from prawdowid.domain.enums import SourceKind
from prawdowid.persistence.models import Source

# apps/backend/src/prawdowid/ingestion/catalog.py -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[5]
DEFAULT_CATALOG = _REPO_ROOT / "data" / "sources" / "catalog.toml"


def catalog_path() -> Path:
    return Path(os.environ.get("PRAWDOWID_CATALOG", DEFAULT_CATALOG))


class CollectSpec(BaseModel):
    model_config = ConfigDict(extra="allow")  # extra keys are collector parameters

    collector: str = Field(min_length=1)

    @property
    def params(self) -> dict[str, Any]:
        return dict(self.model_extra or {})


class SourceEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$", max_length=100)
    name: str = Field(min_length=1)
    publisher: str = Field(min_length=1)
    kind: SourceKind
    domain: str = Field(min_length=3)
    tier: int = Field(ge=1, le=3)
    license: str = Field(min_length=1)
    inclusion_rule: str = Field(min_length=1)
    homepage_url: str | None = None
    status_warning: str | None = None
    lang: str = "pl"
    collect: CollectSpec

    @field_validator("domain")
    @classmethod
    def bare_domain(cls, value: str) -> str:
        value = value.strip().lower()
        if "/" in value or ":" in value:
            raise ValueError("domain must be a bare host name, e.g. 'gov.pl'")
        return value


class Catalog(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: list[SourceEntry]

    @model_validator(mode="after")
    def unique_slugs(self) -> "Catalog":
        slugs = [entry.slug for entry in self.source]
        duplicates = {slug for slug in slugs if slugs.count(slug) > 1}
        if duplicates:
            raise ValueError(f"Duplicate source slugs: {sorted(duplicates)}")
        return self

    def get(self, slug: str) -> SourceEntry:
        for entry in self.source:
            if entry.slug == slug:
                return entry
        raise KeyError(f"Unknown source slug: {slug}")


def load_catalog(path: Path | None = None) -> Catalog:
    with (path or catalog_path()).open("rb") as file:
        return Catalog.model_validate(tomllib.load(file))


def sync_catalog(session: Session, catalog: Catalog) -> dict[str, int]:
    """Upsert catalogue entries by slug; sources no longer listed become inactive.

    Rows are never deleted, so documents keep pointing at the source they came from.
    """
    now = datetime.now(timezone.utc)
    existing = {source.slug: source for source in session.scalars(select(Source))}
    stats = {"added": 0, "updated": 0, "deactivated": 0}
    for entry in catalog.source:
        values = entry.model_dump(exclude={"collect", "slug"})
        source = existing.get(entry.slug)
        if source is None:
            session.add(Source(slug=entry.slug, active=True, reviewed_at=now, **values))
            stats["added"] += 1
            continue
        changed = not source.active or any(getattr(source, k) != v for k, v in values.items())
        if changed:
            for key, value in values.items():
                setattr(source, key, value)
            source.active = True
            source.reviewed_at = now
            stats["updated"] += 1
    listed = {entry.slug for entry in catalog.source}
    for slug, source in existing.items():
        if slug not in listed and source.active:
            source.active = False
            source.reviewed_at = now
            stats["deactivated"] += 1
    session.flush()
    return stats
