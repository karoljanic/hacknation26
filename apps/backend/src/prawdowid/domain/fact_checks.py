from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator


class SearchOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    language_code: str | None = None
    page_size: int = Field(default=10, ge=1, le=100)
    page_token: str | None = None
    offset: int | None = Field(default=None, ge=0)

    @field_validator("language_code", "page_token")
    @classmethod
    def reject_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("Search option must not be blank")
        return value

    @model_validator(mode="after")
    def validate_pagination(self) -> "SearchOptions":
        if self.page_token is not None and self.offset is not None:
            raise ValueError("Use either page_token or offset, not both")
        return self


class TextSearchRequest(SearchOptions):
    query: str | None = None
    publisher_site: str | None = None
    max_age_days: int | None = Field(default=None, ge=0)

    @field_validator("query", "publisher_site")
    @classmethod
    def reject_blank_query(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("Query and publisher must not be blank")
        return value

    @model_validator(mode="after")
    def require_search_term(self) -> "TextSearchRequest":
        if self.query is None and self.publisher_site is None:
            raise ValueError("A query or publisher_site is required")
        return self


class ImageSearchRequest(SearchOptions):
    image_url: HttpUrl

    @field_validator("image_url")
    @classmethod
    def reject_credentials(cls, value: HttpUrl) -> HttpUrl:
        if value.username is not None or value.password is not None:
            raise ValueError("Image URL must not contain credentials")
        return value


class FactCheckPublisher(BaseModel):
    name: str | None = None
    site: str | None = None


class FactCheckReview(BaseModel):
    url: HttpUrl
    publisher: FactCheckPublisher | None = None
    title: str | None = None
    reviewed_at: datetime | None = None
    textual_rating: str | None = None
    language_code: str | None = None


class FactCheckClaim(BaseModel):
    text: str = Field(min_length=1)
    claimant: str | None = None
    claimed_at: datetime | None = None
    reviews: list[FactCheckReview] = Field(default_factory=list)


class FactCheckPage(BaseModel):
    claims: list[FactCheckClaim]
    next_page_token: str | None
    fetched_at: datetime


class FactCheckSource(Protocol):
    def search_text(self, request: TextSearchRequest) -> FactCheckPage: ...

    def search_image(self, request: ImageSearchRequest) -> FactCheckPage: ...


class ClaimFactCheckMatch(BaseModel):
    claim_id: str
    query: str
    page: FactCheckPage


class ClaimFactCheckSearch(BaseModel):
    matches: list[ClaimFactCheckMatch]
    skipped_opinion_ids: list[str]
