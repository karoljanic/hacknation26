"""Fact-check reviews (ClaimReview) of one publisher from the Google Fact Check API.

The API returns review metadata, not article text. Each review becomes a short
document stating the checked claim, who made it and the publisher's own,
unmodified rating. The rating is not turned into a verdict here.
"""

from collections.abc import Iterator, Mapping
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from prawdowid.domain.fact_checks import FactCheckClaim, FactCheckReview, FactCheckSource, TextSearchRequest
from prawdowid.ingestion.domain import CorpusDocument, Skipped


class GoogleFactCheckCollector:
    """params: ``publisher_site`` (required), ``language_code`` (default "pl"),
    ``query`` (optional), ``max_age_days``, ``page_size`` (≤100), ``max_pages``."""

    def __init__(self, source: FactCheckSource) -> None:
        self.source = source

    def collect(self, params: Mapping[str, Any]) -> Iterator[CorpusDocument | Skipped]:
        site = str(params["publisher_site"]).lower()
        max_pages = int(params.get("max_pages", 3))
        token: str | None = None
        seen: set[str] = set()
        for _ in range(max_pages):
            page = self.source.search_text(
                TextSearchRequest(
                    query=params.get("query"),
                    publisher_site=site,
                    language_code=params.get("language_code", "pl"),
                    max_age_days=params.get("max_age_days"),
                    page_size=int(params.get("page_size", 50)),
                    page_token=token,
                )
            )
            for claim in page.claims:
                for review in claim.reviews:
                    url = str(review.url)
                    if url in seen:
                        continue
                    seen.add(url)
                    try:
                        yield to_corpus(claim, review, fetched_at=page.fetched_at)
                    except ValidationError as exc:
                        yield Skipped(reason=f"invalid review: {exc.error_count()} errors", ref=url)
            token = page.next_page_token
            if token is None:
                break


def to_corpus(
    claim: FactCheckClaim, review: FactCheckReview, *, fetched_at: datetime
) -> CorpusDocument | Skipped:
    url = str(review.url)
    if not review.textual_rating:
        return Skipped(reason="review without rating", ref=url)
    publisher = review.publisher.name if review.publisher and review.publisher.name else None
    site = review.publisher.site.lower() if review.publisher and review.publisher.site else None
    lines = [f"Sprawdzana teza: {claim.text}"]
    if claim.claimant:
        lines.append(f"Autor tezy: {claim.claimant}")
    if claim.claimed_at:
        lines.append(f"Data tezy: {claim.claimed_at.date().isoformat()}")
    lines.append(f"Ocena{f' ({publisher})' if publisher else ''}: {review.textual_rating}")
    metadata: dict[str, Any] = {
        "claim_text": claim.text,
        "textual_rating": review.textual_rating,
        "via": "google_fact_check",
    }
    if claim.claimant:
        metadata["claimant"] = claim.claimant
    if claim.claimed_at:
        metadata["claimed_at"] = claim.claimed_at.isoformat()
    if publisher:
        metadata["publisher_name"] = publisher
    return CorpusDocument(
        url=url,
        title=review.title or claim.text[:200],
        text="\n".join(lines),
        fetched_at=fetched_at,
        published_at=review.reviewed_at,
        lang=review.language_code or "pl",
        publisher_site=site,
        metadata=metadata,
    )
