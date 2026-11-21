"""Evidence search: always query Google Fact Check and EUvsDisinfo in parallel
for every non-opinion claim.

Each provider is called independently per claim; a provider outage for one
claim does not block the other provider or other claims, but is recorded in
``ClaimEvidence.unavailable_sources`` so it can be surfaced as an explicit
technical limitation later, never silently turned into "no evidence found".
"""

import asyncio
import logging
import re
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, ValidationError

from prawdowid.adapters.sources.euvsdisinfo import EUvsDisinfoAdapter
from prawdowid.domain.euvsdisinfo import EUvsDisinfoSearchRequest
from prawdowid.domain.evidence import (
    ClaimEvidence,
    ClaimForVerification,
    EvidenceItem,
    EvidenceSearchResult,
    EvidenceSource,
)
from prawdowid.domain.fact_checks import FactCheckSource, TextSearchRequest
from prawdowid.services.gov_connectors import GovConnectorFetcher, GovConnectorSelector

MAX_CLAIMS_PER_SEARCH = 10
MIN_KEYWORDS, MAX_KEYWORDS = 2, 6

# Question words, auxiliaries and fact-checker meta words carry no topic.
_STOPWORDS = frozenset(
    "czy jest był była było byli są nie się że który która które którym którzy jak dla oraz albo lub "
    "tylko jeszcze przez przed można mają miał miała mieli ma w z na do od po za ze to ten ta tym tej "
    "tego dlaczego czemu gdzie kiedy ile jaki jaka jakie demagog demagoga sprawdzał sprawdzali "
    "sprawdzało analizie analiza".split()
)
_WORD = re.compile(r"[\wąćęłńóśźżĄĆĘŁŃÓŚŹŻ%-]+")

log = logging.getLogger(__name__)


def _bare_host(site: str) -> str:
    """Strips scheme/path/``www.`` for matching against a catalogued Source domain."""
    site = site.strip().lower()
    if "://" in site:
        site = site.split("://", 1)[1]
    site = site.split("/", 1)[0]
    return site.removeprefix("www.")


class _EUvsDisinfoSearchHit(BaseModel):
    type: str = Field(min_length=1)
    title: str = Field(min_length=1)
    content: str = Field(min_length=1)
    link: str = Field(min_length=1)


class _EUvsDisinfoSearchResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    results: list[_EUvsDisinfoSearchHit]


def search_queries(text: str) -> list[str]:
    """Full text plus short keyword variants.

    Google Fact Check matches rigidly: a full sentence often returns nothing,
    while the same article is found by 2-6 of its topic words. Which length
    hits varies per claim, so several variants are tried and merged.
    """
    words = [
        word
        for word in _WORD.findall(text)
        if word.casefold() not in _STOPWORDS and (len(word) > 3 or word.isdigit())
    ]
    queries = [text]
    for count in range(MIN_KEYWORDS, MAX_KEYWORDS + 1):
        query = " ".join(words[:count])
        if query and query not in queries:
            queries.append(query)
    return queries


def _google_items(query: str, source: FactCheckSource, *, language_code: str, page_size: int) -> list[EvidenceItem]:
    page = source.search_text(TextSearchRequest(query=query, language_code=language_code, page_size=page_size))
    items: list[EvidenceItem] = []
    for claim in page.claims:
        for review in claim.reviews:
            items.append(
                EvidenceItem(
                    source="google_fact_check",
                    kind="claim_review",
                    title=review.title or claim.text[:200],
                    url=review.url,
                    publisher=review.publisher.name if review.publisher else None,
                    publisher_site=_bare_host(review.publisher.site) if review.publisher and review.publisher.site else None,
                    rating=review.textual_rating,
                    snippet=claim.text,
                    published_at=review.reviewed_at or claim.claimed_at,
                )
            )
    return items


async def _euvsdisinfo_items(query: str, adapter: EUvsDisinfoAdapter) -> list[EvidenceItem]:
    result = await adapter.search(EUvsDisinfoSearchRequest(query=query))
    if result.structured_content is None:
        raise RuntimeError("EUvsDisinfo search returned no structured_content; cannot trust its shape")
    try:
        parsed = _EUvsDisinfoSearchResponse.model_validate(result.structured_content)
    except ValidationError as error:
        raise RuntimeError("EUvsDisinfo search returned an unexpected shape") from error
    items: list[EvidenceItem] = []
    for hit in parsed.results:
        kind = "claim_review" if hit.type == "claim_review" else "article"
        try:
            url = HttpUrl(hit.link)
        except ValidationError:
            continue  # a malformed link is skipped, not fabricated into evidence
        items.append(
            EvidenceItem(
                source="euvsdisinfo",
                kind=kind,
                title=hit.title,
                url=url,
                publisher="EUvsDisinfo",
                publisher_site="euvsdisinfo.eu",
                # claim_review here means a catalogued disinformation case: the
                # text is the false/misleading narrative EUvsDisinfo identified
                # and debunked, never a confirmed fact by itself.
                rating="Narracja uznana przez EUvsDisinfo za dezinformację" if kind == "claim_review" else None,
                snippet=hit.content[:800],
                published_at=None,  # the search tool does not document a date field
            )
        )
    return items


class EvidenceSearchOrchestrator:
    def __init__(
        self,
        *,
        google: FactCheckSource,
        euvsdisinfo: EUvsDisinfoAdapter,
        gov_selector: GovConnectorSelector | None = None,
        gov_fetcher: GovConnectorFetcher | None = None,
        language_code: str = "pl",
        page_size: int = 5,
        max_claims: int = MAX_CLAIMS_PER_SEARCH,
    ) -> None:
        self.google = google
        self.euvsdisinfo = euvsdisinfo
        self.gov_selector = gov_selector
        self.gov_fetcher = gov_fetcher or (GovConnectorFetcher() if gov_selector is not None else None)
        self.language_code = language_code
        self.page_size = page_size
        self.max_claims = max_claims

    async def search(self, claims: Sequence[ClaimForVerification]) -> EvidenceSearchResult:
        if len({claim.id for claim in claims}) != len(claims):
            raise ValueError("Claim IDs must be unique")
        candidates = [claim for claim in claims if claim.kind != "opinion"]
        if len(candidates) > self.max_claims:
            raise ValueError("Evidence search exceeds the claim limit; split the input into batches")
        results = await asyncio.gather(*(self._search_claim(claim) for claim in candidates))
        return EvidenceSearchResult(
            claims=list(results),
            skipped_opinion_ids=[claim.id for claim in claims if claim.kind == "opinion"],
        )

    async def _google_search(self, text: str) -> list[EvidenceItem]:
        results = await asyncio.gather(
            *(
                asyncio.to_thread(
                    _google_items, query, self.google, language_code=self.language_code, page_size=self.page_size
                )
                for query in search_queries(text)
            ),
            return_exceptions=True,
        )
        failures = [result for result in results if isinstance(result, BaseException)]
        if len(failures) == len(results):
            raise failures[0]
        merged: dict[str, EvidenceItem] = {}
        for result in results:
            if not isinstance(result, BaseException):
                for item in result:
                    merged.setdefault(str(item.url), item)
        return list(merged.values())

    async def _search_claim(self, claim: ClaimForVerification) -> ClaimEvidence:
        google_task = self._google_search(claim.text)
        euvsdisinfo_task = _euvsdisinfo_items(claim.text, self.euvsdisinfo)
        google_result, euvsdisinfo_result = await asyncio.gather(
            google_task, euvsdisinfo_task, return_exceptions=True
        )
        items: list[EvidenceItem] = []
        unavailable: list[EvidenceSource] = []
        for source, result in (("google_fact_check", google_result), ("euvsdisinfo", euvsdisinfo_result)):
            if isinstance(result, BaseException):
                unavailable.append(source)  # type: ignore[arg-type]
                continue
            items.extend(result)
        if self.gov_selector is not None:
            # Best-effort enhancement, not an always-on source: a partial or
            # total fetch failure here degrades gracefully (fewer items),
            # it does not fail the claim or mark it "unavailable".
            items.extend(await asyncio.to_thread(self._gov_items, claim.text))
        return ClaimEvidence(claim_id=claim.id, items=items, unavailable_sources=unavailable)

    def _gov_items(self, claim_text: str) -> list[EvidenceItem]:
        assert self.gov_selector is not None and self.gov_fetcher is not None
        try:
            selected = self.gov_selector.select(claim_text)
            if not selected:
                return []
            return self.gov_fetcher.fetch_many(selected).items
        except Exception:
            log.warning("gov.pl connector step failed", exc_info=True)
            return []
