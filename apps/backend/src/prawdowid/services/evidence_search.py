"""Evidence search: for every non-opinion claim, query in parallel Google Fact
Check and EUvsDisinfo (always), gov.pl site search, MSZ Odyseusz travel
advisories for the foreign countries the claim names (when configured), plus the catalogued gov.pl sections a decision step picks.

Each provider is called independently per claim; a provider outage for one
claim does not block the other providers or other claims, but is recorded in
``ClaimEvidence.unavailable_sources`` so it can be surfaced as an explicit
technical limitation later, never silently turned into "no evidence found".
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, time

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, ValidationError

from prawdowid.adapters.sources.euvsdisinfo import EUvsDisinfoAdapter
from prawdowid.adapters.sources.gov_api import CountryAdvisory, GovPlSearchClient, GovSearchHit, OdyseuszClient
from prawdowid.domain.euvsdisinfo import EUvsDisinfoSearchRequest
from prawdowid.domain.evidence import (
    ClaimEvidence,
    ClaimForVerification,
    EvidenceItem,
    EvidenceSearchResult,
    EvidenceSource,
)
from prawdowid.domain.fact_checks import FactCheckSource, TextSearchRequest
from prawdowid.services.relevance import claim_terms, keywords, terms
from prawdowid.services.gov_connectors import GovConnectorFetcher, GovConnectorSelector

MAX_CLAIMS_PER_SEARCH = 10
MIN_KEYWORDS, MAX_KEYWORDS = 2, 6
MAX_GOOGLE_QUERIES = 10  # Google Fact Check calls per claim
MAX_FULL_TEXT_QUERY = 300  # longer quotes are only searched by keywords
GOV_SEARCH_HITS_PER_QUERY = 10
GOV_SEARCH_MAX_ITEMS = 5
# gov.pl search ORs the query words, so "strajk generalny Włochy" also returns
# every "Dyrektor Generalny" page; a hit must share most of the query's terms.
GOV_SEARCH_MIN_TERM_OVERLAP = 0.6
GOV_SEARCH_SLUG = "gov-pl-search"
ODYSEUSZ_SLUG = "msz-odyseusz"
ODYSEUSZ_PUBLISHER = "Ministerstwo Spraw Zagranicznych (Odyseusz)"

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


def _salience(word: str) -> float:
    """Names ("Wrocław") and number-bearing words ("65-latkowie") identify a
    topic far better than common words; bare numbers are weak on their own
    (fact-checks often round or restate them)."""
    has_digit = any(ch.isdigit() for ch in word)
    has_alpha = any(ch.isalpha() for ch in word)
    score = min(len(word), 12) / 12
    if word[:1].isupper():
        score += 3
    if has_digit and has_alpha:
        score += 3
    elif has_digit:
        score += 0.5
    return score


def _keyword_variants(text: str) -> list[str]:
    words = keywords(text)
    ranked = sorted(range(len(words)), key=lambda i: (-_salience(words[i]), i))
    variants: list[str] = []
    # Most salient words, kept in their original order (Google is word-order sensitive).
    for count in range(MIN_KEYWORDS, MIN_KEYWORDS + 3):
        variants.append(" ".join(words[i] for i in sorted(ranked[:count])))
    # Leading keywords: the subject and verb usually come first in Polish claims.
    for count in (MIN_KEYWORDS + 1, MAX_KEYWORDS):
        variants.append(" ".join(words[:count]))
    return variants


def search_queries(
    text: str,
    hints: Sequence[str] = (),
    source_quotes: Sequence[str] = (),
    *,
    max_queries: int = MAX_GOOGLE_QUERIES,
) -> list[str]:
    """Full text, model-written keyword queries, then keyword variants of the
    claim and of the user's original wording.

    Google Fact Check matches rigidly: a full sentence often returns nothing,
    while the same article is found by 2-4 of its topic words, and only when
    they match the article's wording. ``hints`` (from claim extraction) add
    synonyms; the extractor also rewords claims, so the user's verbatim
    ``source_quotes`` are searched too. Which query hits varies per claim, so
    several are tried and their results merged by URL.
    """
    bases = [text, *(quote for quote in source_quotes if quote.strip() and quote.strip() != text.strip())]
    candidates: list[str] = []
    for base in bases[:1]:
        candidates.append(base.strip())
    candidates.extend(hints)
    for base in bases[1:]:
        if len(base) <= MAX_FULL_TEXT_QUERY:
            candidates.append(base.strip())
    # Interleave so the cap still keeps the best variants of every base text.
    per_base = [_keyword_variants(base) for base in bases]
    for rank in range(max((len(v) for v in per_base), default=0)):
        candidates.extend(variants[rank] for variants in per_base if rank < len(variants))
    queries: list[str] = []
    for query in candidates:
        if query and query.casefold() not in {q.casefold() for q in queries}:
            queries.append(query)
    return queries[:max_queries]


def is_relevant_hit(query: str, text: str) -> bool:
    wanted = terms(query)
    if not wanted:
        return False
    return len(wanted & terms(text)) / len(wanted) >= GOV_SEARCH_MIN_TERM_OVERLAP


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
        lang="pl",
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
                snippet=advisory.notice, published_at=published, lang="pl",
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
                snippet=profile, published_at=published, lang="pl",
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
                    # The review's language as reported by Google; queries are
                    # restricted to ``language_code`` anyway.
                    lang=(review.language_code or language_code)[:8] or None,
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
                lang="en",  # the registry's case descriptions and articles are in English
            )
        )
    return items


class ProviderNotConfigured(RuntimeError):
    pass


async def _unconfigured(source: str) -> list[EvidenceItem]:
    raise ProviderNotConfigured(f"{source} is not configured")


def _contains_external_cancellation(group: BaseExceptionGroup) -> bool:
    """A group holding only a CancelledError means the check itself was
    cancelled (e.g. the client went away): that must propagate."""
    return all(isinstance(error, asyncio.CancelledError) for error in group.exceptions)


def _with_euvsdisinfo(
    claim_evidence: ClaimEvidence, result: list[EvidenceItem] | BaseException
) -> ClaimEvidence:
    if isinstance(result, BaseException):
        return claim_evidence.model_copy(
            update={"unavailable_sources": [*claim_evidence.unavailable_sources, "euvsdisinfo"]}
        )
    # Evidence order: Google, EUvsDisinfo, then the other providers.
    google = [item for item in claim_evidence.items if item.source == "google_fact_check"]
    rest = [item for item in claim_evidence.items if item.source != "google_fact_check"]
    return claim_evidence.model_copy(update={"items": [*google, *result, *rest]})


EUvsDisinfoConnect = Callable[[], AbstractAsyncContextManager[EUvsDisinfoAdapter]]


class EvidenceSearchOrchestrator:
    def __init__(
        self,
        *,
        google: FactCheckSource | None,
        euvsdisinfo: EUvsDisinfoAdapter | None = None,
        euvsdisinfo_connect: EUvsDisinfoConnect | None = None,
        gov_selector: GovConnectorSelector | None = None,
        gov_fetcher: GovConnectorFetcher | None = None,
        gov_search: GovPlSearchClient | None = None,
        odyseusz: OdyseuszClient | None = None,
        language_code: str = "pl",
        page_size: int = 5,
        max_claims: int = MAX_CLAIMS_PER_SEARCH,
    ) -> None:
        """EUvsDisinfo is given either as an open adapter (tests) or, in
        production, as ``euvsdisinfo_connect``: a factory for the MCP
        connection, which is then opened only for the duration of the
        EUvsDisinfo queries (see ``_euvsdisinfo_batch``)."""
        if euvsdisinfo is not None and euvsdisinfo_connect is not None:
            raise ValueError("Pass either an EUvsDisinfo adapter or a connection factory, not both")
        self.google = google
        self.euvsdisinfo = euvsdisinfo
        self.euvsdisinfo_connect = euvsdisinfo_connect
        self.gov_selector = gov_selector
        self.gov_fetcher = gov_fetcher or (GovConnectorFetcher() if gov_selector is not None else None)
        self.gov_search = gov_search
        self.odyseusz = odyseusz
        self.language_code = language_code
        self.page_size = page_size
        self.max_claims = max_claims

    async def search(self, claims: Sequence[ClaimForVerification]) -> EvidenceSearchResult:
        if len({claim.id for claim in claims}) != len(claims):
            raise ValueError("Claim IDs must be unique")
        candidates = [claim for claim in claims if claim.kind != "opinion"]
        if len(candidates) > self.max_claims:
            raise ValueError("Evidence search exceeds the claim limit; split the input into batches")
        euvsdisinfo_results, *results = await asyncio.gather(
            self._euvsdisinfo_batch(candidates), *(self._search_claim(claim) for claim in candidates)
        )
        merged = [
            _with_euvsdisinfo(claim_evidence, euvsdisinfo_results[claim_evidence.claim_id])
            for claim_evidence in results
        ]
        return EvidenceSearchResult(
            claims=merged,
            skipped_opinion_ids=[claim.id for claim in claims if claim.kind == "opinion"],
        )

    async def _euvsdisinfo_batch(
        self, claims: Sequence[ClaimForVerification]
    ) -> dict[str, list[EvidenceItem] | BaseException]:
        """All EUvsDisinfo queries of one check, isolated from the rest of it.

        The MCP client sends requests from a background task group whose
        cancel scope covers everything inside ``connect()``. A transport
        failure (DNS, connection reset) is not raised from ``search()``: it
        cancels that whole scope and surfaces as an ``ExceptionGroup`` on exit.
        Holding the connection open around the whole check therefore let one
        unreachable source cancel the entire request. Here the scope covers
        only these queries, and any failure marks EUvsDisinfo unavailable.
        """
        if not claims:
            return {}
        if self.euvsdisinfo is None and self.euvsdisinfo_connect is None:
            return {claim.id: ProviderNotConfigured("euvsdisinfo is not configured") for claim in claims}
        try:
            if self.euvsdisinfo is not None:
                return await self._euvsdisinfo_queries(self.euvsdisinfo, claims)
            assert self.euvsdisinfo_connect is not None
            async with self.euvsdisinfo_connect() as adapter:
                return await self._euvsdisinfo_queries(adapter, claims)
        except (Exception, BaseExceptionGroup) as error:  # noqa: B036 - anyio wraps transport errors
            if not isinstance(error, Exception) and _contains_external_cancellation(error):
                raise
            log.warning("EUvsDisinfo unavailable for this check", exc_info=True)
            return {claim.id: error for claim in claims}

    @staticmethod
    async def _euvsdisinfo_queries(
        adapter: EUvsDisinfoAdapter, claims: Sequence[ClaimForVerification]
    ) -> dict[str, list[EvidenceItem] | BaseException]:
        results = await asyncio.gather(
            # The registry is written in English; a Polish query matches it poorly.
            *(_euvsdisinfo_items(claim.text_en or claim.text, adapter) for claim in claims),
            return_exceptions=True,
        )
        return {claim.id: result for claim, result in zip(claims, results, strict=True)}

    async def _google_search(self, claim: ClaimForVerification) -> list[EvidenceItem]:
        results = await asyncio.gather(
            *(
                asyncio.to_thread(
                    _google_items, query, self.google, language_code=self.language_code, page_size=self.page_size
                )
                for query in search_queries(claim.text, claim.search_queries, claim.source_quotes)
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

    async def _search_claim(self, claim: ClaimForVerification) -> ClaimEvidence:
        # Each provider runs concurrently; result order fixes the evidence order.
        # A provider that is not configured (missing key) is reported exactly
        # like an outage: an explicit limitation, never "nothing found".
        # EUvsDisinfo is queried for all claims at once (``_euvsdisinfo_batch``)
        # and merged in by ``search``.
        providers: dict[EvidenceSource, Awaitable[list[EvidenceItem]]] = {
            "google_fact_check": self._google_search(claim)
            if self.google is not None
            else _unconfigured("google_fact_check"),
        }
        countries = [code for code in claim.countries if code != "PL"]
        if self.odyseusz is not None and countries:
            providers["odyseusz"] = asyncio.to_thread(self._odyseusz_items, countries)
        if self.gov_search is not None:
            providers["gov_pl_search"] = asyncio.to_thread(self._gov_search_items, claim)
        gov_task = asyncio.to_thread(self._gov_items, claim) if self.gov_selector is not None else None

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

    def _gov_items(self, claim: ClaimForVerification) -> list[EvidenceItem]:
        assert self.gov_selector is not None and self.gov_fetcher is not None
        try:
            selected = self.gov_selector.select(claim.text)
            if not selected:
                return []
            wanted = claim_terms([claim.text, *claim.search_queries])
            return self.gov_fetcher.fetch_many(selected, wanted, claim_text=claim.text).items
        except Exception:
            log.warning("gov.pl connector step failed", exc_info=True)
            return []
