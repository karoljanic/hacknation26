from datetime import datetime, timezone

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from prawdowid.domain.fact_checks import (
    FactCheckClaim,
    FactCheckPage,
    FactCheckPublisher,
    FactCheckReview,
    ImageSearchRequest,
    SearchOptions,
    TextSearchRequest,
)


class FactCheckAPIError(RuntimeError):
    def __init__(self, status_code: int, retry_after: str | None) -> None:
        super().__init__(f"Google Fact Check request failed with HTTP {status_code}")
        self.status_code = status_code
        self.retry_after = retry_after


class _GoogleReview(BaseModel):
    url: str
    publisher: FactCheckPublisher | None = None
    title: str | None = None
    reviewed_at: datetime | None = Field(default=None, alias="reviewDate")
    textual_rating: str | None = Field(default=None, alias="textualRating")
    language_code: str | None = Field(default=None, alias="languageCode")


class _GoogleClaim(BaseModel):
    text: str = Field(min_length=1)
    claimant: str | None = None
    claimed_at: datetime | None = Field(default=None, alias="claimDate")
    reviews: list[_GoogleReview] = Field(default_factory=list, alias="claimReview")

    def to_domain(self) -> FactCheckClaim:
        return FactCheckClaim(
            text=self.text,
            claimant=self.claimant,
            claimed_at=self.claimed_at,
            reviews=[FactCheckReview.model_validate(review.model_dump()) for review in self.reviews],
        )


class _TextResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claims: list[_GoogleClaim] = Field(default_factory=list)
    next_page_token: str | None = Field(default=None, alias="nextPageToken")


class _ImageResult(BaseModel):
    claim: _GoogleClaim


class _ImageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    results: list[_ImageResult] = Field(default_factory=list)
    next_page_token: str | None = Field(default=None, alias="nextPageToken")


class GoogleFactCheckAdapter:
    def __init__(self, *, client: httpx.Client, api_key: str) -> None:
        if not api_key.strip():
            raise ValueError("Google Fact Check API key must not be empty")
        self.client = client
        self._api_key = api_key

    def search_text(self, request: TextSearchRequest) -> FactCheckPage:
        params = self._options(request)
        if request.query is not None:
            params["query"] = request.query
        if request.publisher_site is not None:
            params["reviewPublisherSiteFilter"] = request.publisher_site
        if request.max_age_days is not None:
            params["maxAgeDays"] = request.max_age_days
        raw = self._get("search", params)
        try:
            response = _TextResponse.model_validate_json(raw)
            claims = [claim.to_domain() for claim in response.claims]
        except ValidationError as error:
            raise RuntimeError("Google Fact Check returned an invalid text search response") from error
        return FactCheckPage(
            claims=claims,
            next_page_token=response.next_page_token or None,
            fetched_at=datetime.now(timezone.utc),
        )

    def search_image(self, request: ImageSearchRequest) -> FactCheckPage:
        params = self._options(request)
        params["imageUri"] = str(request.image_url)
        raw = self._get("imageSearch", params)
        try:
            response = _ImageResponse.model_validate_json(raw)
            claims = [result.claim.to_domain() for result in response.results]
        except ValidationError as error:
            raise RuntimeError("Google Fact Check returned an invalid image search response") from error
        return FactCheckPage(
            claims=claims,
            next_page_token=response.next_page_token or None,
            fetched_at=datetime.now(timezone.utc),
        )

    @staticmethod
    def _options(request: SearchOptions) -> dict[str, str | int]:
        params: dict[str, str | int] = {"pageSize": request.page_size}
        if request.language_code is not None:
            params["languageCode"] = request.language_code
        if request.page_token is not None:
            params["pageToken"] = request.page_token
        if request.offset is not None:
            params["offset"] = request.offset
        return params

    def _get(self, method: str, params: dict[str, str | int]) -> bytes:
        with self.client.stream(
            "GET",
            f"https://factchecktools.googleapis.com/v1alpha1/claims:{method}",
            params=params,
            headers={"X-Goog-Api-Key": self._api_key, "Accept": "application/json"},
            timeout=10,
            follow_redirects=False,
        ) as response:
            if response.status_code != 200:
                raise FactCheckAPIError(response.status_code, response.headers.get("Retry-After"))
            body = bytearray()
            for chunk in response.iter_bytes(chunk_size=65536):
                body.extend(chunk)
                if len(body) > 2_000_000:
                    raise RuntimeError("Google Fact Check response exceeds the 2 MB limit")
        return bytes(body)
