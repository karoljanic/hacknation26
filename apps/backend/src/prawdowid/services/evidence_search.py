"""Evidence search: for every non-opinion claim, query in parallel Google Fact
Check and EUvsDisinfo (always), gov.pl site search, MSZ Odyseusz travel
advisories for the foreign countries the claim names and Polish Wikipedia
(when configured), plus the catalogued gov.pl sections a decision step picks.

Each provider is called independently per claim; a provider outage for one
claim does not block the other providers or other claims, but is recorded in
``ClaimEvidence.unavailable_sources`` so it can be surfaced as an explicit
technical limitation later, never silently turned into "no evidence found".
"""

import asyncio
import logging
import re
from collections.abc import Awaitable, Sequence
from datetime import UTC, datetime, time

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, ValidationError

from prawdowid.adapters.sources.euvsdisinfo import EUvsDisinfoAdapter
from prawdowid.adapters.sources.gov_api import CountryAdvisory, GovPlSearchClient, GovSearchHit, OdyseuszClient
from prawdowid.adapters.sources.wikipedia import WikipediaClient
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
MAX_GOOGLE_QUERIES = 8
GOV_SEARCH_HITS_PER_QUERY = 10
GOV_SEARCH_MAX_ITEMS = 5
# gov.pl search ORs the query words, so "strajk generalny Włochy" also returns
# every "Dyrektor Generalny" page; a hit must share most of the query's terms.
GOV_SEARCH_MIN_TERM_OVERLAP = 0.6
GOV_SEARCH_SLUG = "gov-pl-search"
ODYSEUSZ_SLUG = "msz-odyseusz"
ODYSEUSZ_PUBLISHER = "Ministerstwo Spraw Zagranicznych (Odyseusz)"
WIKIPEDIA_SLUG = "wikipedia-pl"
WIKIPEDIA_QUERIES = 2
WIKIPEDIA_HITS_PER_QUERY = 2
WIKIPEDIA_MAX_ARTICLES = 3

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


def _keywords(text: str) -> list[str]:
    return [
        word
        for word in _WORD.findall(text)
        if word.casefold() not in _STOPWORDS and (len(word) > 3 or word.isdigit())
    ]


def search_queries(text: str, hints: Sequence[str] = ()) -> list[str]:
    """Full text, model-written keyword queries, then short keyword variants.

    Google Fact Check matches rigidly: a full sentence often returns nothing,
    while the same article is found by 2-6 of its topic words, and only when
    they match the article's wording (``hints`` from claim extraction add
    synonyms). Which query hits varies per claim, so several are tried.
    """
    queries = [text]
    for hint in hints:
        if hint not in queries:
            queries.append(hint)
    words = _keywords(text)
    for count in range(MIN_KEYWORDS, MAX_KEYWORDS + 1):
        query = " ".join(words[:count])
        if query and query not in queries:
            queries.append(query)
    return queries[:MAX_GOOGLE_QUERIES]


def _terms(text: str) -> set[str]:
    # A 5-letter prefix is a crude stem that survives most Polish inflection.
    return {word.casefold()[:5] for word in _keywords(text)}


def is_relevant_hit(query: str, text: str) -> bool:
    wanted = _terms(query)
    if not wanted:
        return False
    return len(wanted & _terms(text)) / len(wanted) >= GOV_SEARCH_MIN_TERM_OVERLAP


def _merge_or_raise(results: Sequence[list[EvidenceItem] | BaseException]) -> list[EvidenceItem]:
    """Merges several queries' items by URL; raises only if every query failed."""
    failures = [result for result in results if isinstance(result, BaseException)]
    if results and len(failures) == len(results):
        raise failures[0]
    merged: dict[str, EvidenceItem] = {}
    for result in results:
        if not isinstance(result, BaseException):
            for item in result:
                merged.setdefault(str(item.url), item)
    return list(merged.values())


def _gov_search_item(hit: GovSearchHit) -> EvidenceItem:
    return EvidenceItem(
        source="gov_pl_search",
        kind="official",
        title=hit.title,
        url=hit.url,
        publisher=f"gov.pl – {hit.section}" if hit.section else "gov.pl",
        catalog_slug=GOV_SEARCH_SLUG,
        snippet=hit.intro or hit.title,
        published_at=hit.published_at,
    )


def odyseusz_items(advisory: CountryAdvisory) -> list[EvidenceItem]:
    published = datetime.combine(advisory.updated_at, time(), tzinfo=UTC) if advisory.updated_at else None
    items: list[EvidenceItem] = []
    if advisory.notice:
        items.append(
            EvidenceItem(
                source="odyseusz", kind="official",
                title=f"Komunikat MSZ dla podróżujących – {advisory.name}",
                url=advisory.page_url, publisher=ODYSEUSZ_PUBLISHER, catalog_slug=ODYSEUSZ_SLUG,
                snippet=advisory.notice, published_at=published,
            )
        )
    profile = "\n".join(
        [f"Ostrzeżenie: {warning}" for warning in advisory.warnings]
        + [f"{topic}: {summary}" for topic, summary in advisory.sections]
    )
    if profile:
        items.append(
            EvidenceItem(
                source="odyseusz", kind="official",
                title=f"Profil kraju MSZ – {advisory.name}: ostrzeżenia i najważniejsze informacje",
                url=f"{advisory.page_url}#profil", publisher=ODYSEUSZ_PUBLISHER, catalog_slug=ODYSEUSZ_SLUG,
                snippet=profile, published_at=published,
            )
        )
    return items


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
        gov_search: GovPlSearchClient | None = None,
        odyseusz: OdyseuszClient | None = None,
        wikipedia: WikipediaClient | None = None,
        language_code: str = "pl",
        page_size: int = 5,
        max_claims: int = MAX_CLAIMS_PER_SEARCH,
    ) -> None:
        self.google = google
        self.euvsdisinfo = euvsdisinfo
        self.gov_selector = gov_selector
        self.gov_fetcher = gov_fetcher or (GovConnectorFetcher() if gov_selector is not None else None)
        self.gov_search = gov_search
        self.odyseusz = odyseusz
        self.wikipedia = wikipedia
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

    async def _google_search(self, claim: ClaimForVerification) -> list[EvidenceItem]:
        results = await asyncio.gather(
            *(
                asyncio.to_thread(
                    _google_items, query, self.google, language_code=self.language_code, page_size=self.page_size
                )
                for query in search_queries(claim.text, claim.search_queries)
            ),
            return_exceptions=True,
        )
        return _merge_or_raise(results)

    def _gov_search_items(self, claim: ClaimForVerification) -> list[EvidenceItem]:
        assert self.gov_search is not None
        queries = list(claim.search_queries) or search_queries(claim.text)[2:4]
        results: list[list[EvidenceItem] | BaseException] = []
        for query in queries:
            try:
                hits = self.gov_search.search(query, size=GOV_SEARCH_HITS_PER_QUERY)
            except Exception as error:  # one failed query must not drop the others' hits
                results.append(error)
                continue
            results.append(
                [_gov_search_item(hit) for hit in hits if is_relevant_hit(query, f"{hit.title} {hit.intro}")]
            )
        return _merge_or_raise(results)[:GOV_SEARCH_MAX_ITEMS]

    def _odyseusz_items(self, countries: Sequence[str]) -> list[EvidenceItem]:
        assert self.odyseusz is not None
        results: list[list[EvidenceItem] | BaseException] = []
        for code in countries:
            try:
                advisory = self.odyseusz.country(code)
            except Exception as error:
                results.append(error)
                continue
            results.append(odyseusz_items(advisory) if advisory else [])
        return _merge_or_raise(results)

    def _wikipedia_items(self, claim: ClaimForVerification) -> list[EvidenceItem]:
        assert self.wikipedia is not None
        queries = list(claim.search_queries[:WIKIPEDIA_QUERIES]) or [claim.text]
        titles: list[str] = []
        failures: list[Exception] = []
        for query in queries:
            try:
                found = self.wikipedia.search(query, limit=WIKIPEDIA_HITS_PER_QUERY)
            except Exception as error:
                failures.append(error)
                continue
            titles.extend(title for title in found if title not in titles)
        if failures and len(failures) == len(queries):
            raise failures[0]
        return [
            EvidenceItem(
                source="wikipedia", kind="encyclopedia", title=article.title, url=article.url,
                publisher="Wikipedia (pl)", catalog_slug=WIKIPEDIA_SLUG, snippet=article.intro,
            )
            for article in self.wikipedia.intros(titles[:WIKIPEDIA_MAX_ARTICLES])
        ]

    async def _search_claim(self, claim: ClaimForVerification) -> ClaimEvidence:
        # Each provider runs concurrently; result order fixes the evidence order.
        providers: dict[EvidenceSource, Awaitable[list[EvidenceItem]]] = {
            "google_fact_check": self._google_search(claim),
            "euvsdisinfo": _euvsdisinfo_items(claim.text, self.euvsdisinfo),
        }
        countries = [code for code in claim.countries if code != "PL"]
        if self.odyseusz is not None and countries:
            providers["odyseusz"] = asyncio.to_thread(self._odyseusz_items, countries)
        if self.gov_search is not None:
            providers["gov_pl_search"] = asyncio.to_thread(self._gov_search_items, claim)
        if self.wikipedia is not None:
            providers["wikipedia"] = asyncio.to_thread(self._wikipedia_items, claim)
        gov_task = asyncio.to_thread(self._gov_items, claim.text) if self.gov_selector is not None else None

        results = await asyncio.gather(*providers.values(), return_exceptions=True)
        items: list[EvidenceItem] = []
        unavailable: list[EvidenceSource] = []
        for source, result in zip(providers, results, strict=True):
            if isinstance(result, BaseException):
                log.warning("Evidence provider %s failed", source, exc_info=result)
                unavailable.append(source)
                continue
            items.extend(result)
        if gov_task is not None:
            # Best-effort enhancement, not an always-on source: a partial or
            # total fetch failure here degrades gracefully (fewer items),
            # it does not fail the claim or mark it "unavailable".
            items.extend(await gov_task)
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
