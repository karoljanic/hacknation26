import json
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from prawdowid.adapters.sources.web import FetchedResource, PublicWebFetcher

STRUCTURED_CONTENT_TYPES = {
    "application/json",
    "application/ld+json",
    "application/xml",
    "application/rss+xml",
    "application/atom+xml",
    "text/xml",
    "text/csv",
    "text/plain",
}


class ResourceFetcher(Protocol):
    def fetch_resource(
        self,
        url: str,
        *,
        allowed_hosts: set[str],
        accepted_content_types: set[str],
        accept_header: str = "*/*",
        content_type_description: str = "resource",
    ) -> FetchedResource: ...


@dataclass(frozen=True)
class StructuredResponse:
    url: str
    status_code: int
    content_type: str
    fetched_at: datetime
    data: object


class PublicStructuredFetcher:
    def __init__(
        self,
        *,
        fetcher: ResourceFetcher | None = None,
        max_bytes: int = 4_000_000,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("Response size limit must be positive")
        self.fetcher = fetcher or PublicWebFetcher(max_bytes=max_bytes)

    def fetch(self, url: str, *, allowed_hosts: set[str]) -> StructuredResponse:
        resource = self.fetcher.fetch_resource(
            url,
            allowed_hosts=allowed_hosts,
            accepted_content_types=STRUCTURED_CONTENT_TYPES,
            accept_header=(
                "application/json, application/ld+json, application/xml, "
                "application/rss+xml, application/atom+xml, text/xml, text/csv, text/plain"
            ),
            content_type_description="structured data",
        )
        if resource.content_type == "application/json" or resource.content_type.endswith("+json"):
            try:
                data = json.loads(resource.body)
            except json.JSONDecodeError as error:
                raise ValueError(f"API returned invalid JSON: {resource.url}") from error
        else:
            encoding = resource.encoding or "utf-8"
            try:
                data = resource.body.decode(encoding)
            except (LookupError, UnicodeDecodeError) as error:
                raise ValueError(f"Could not decode API response: {resource.url}") from error
        return StructuredResponse(
            url=resource.url,
            status_code=resource.status_code,
            content_type=resource.content_type,
            fetched_at=resource.fetched_at,
            data=data,
        )
