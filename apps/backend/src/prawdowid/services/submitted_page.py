"""A link the user submits from a catalogued, trusted source (www.gov.pl, an
official service, a fact-checker) is itself evidence for questions about it.

"Czy ten poradnik zaleca zapas wody?" with a link to the gov.pl safety guide
must be answered from the guide, not only from unrelated search results. The
submitted page's passages about the claim become evidence, and so do up to
``MAX_SUBPAGES`` pages on the same site whose link labels best match the claim
(a guide's landing page is often just an index of topic pages).

Pages from anywhere else (news portals, social media) are what is being
checked, never evidence: they are not used here.
"""

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

from prawdowid.adapters.sources.site_crawler import describe_html
from prawdowid.adapters.sources.web import FetchedArticle, PublicWebFetcher
from prawdowid.domain.evidence import EvidenceItem
from prawdowid.ingestion.catalog import SourceEntry
from prawdowid.services.gov_connectors import SourceDocument, read_official_page
from prawdowid.services.relevance import best_passages, score

MAX_SUBPAGES = 2
GOV_PL_HOSTS = frozenset({"www.gov.pl", "gov.pl"})
GOV_PL_SLUG = "gov-pl-search"
# Not citable as pages: a disinformation-case registry and a search aggregator.
_NOT_PAGE_SOURCES = frozenset({"euvsdisinfo", "google_fact_check_live"})

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TrustedSite:
    slug: str
    publisher: str
    domain: str
    kind: str


def trusted_site(url: str, entries: Sequence[SourceEntry]) -> TrustedSite | None:
    host = (urlsplit(url).hostname or "").lower()
    by_slug = {entry.slug: entry for entry in entries}
    if host in GOV_PL_HOSTS and GOV_PL_SLUG in by_slug:
        return TrustedSite(GOV_PL_SLUG, "gov.pl", "gov.pl", "official")
    for entry in entries:
        domain = entry.domain.lower()
        # "gov.pl" is shared by every gov.pl section: handled above, never by suffix.
        if entry.collect.collector in _NOT_PAGE_SOURCES or domain == "gov.pl":
            continue
        if host in (domain, f"www.{domain}"):
            return TrustedSite(entry.slug, entry.publisher, domain, entry.kind)
    return None


class SubmittedPageEvidence:
    def __init__(
        self,
        *,
        entries: Sequence[SourceEntry],
        link_fetcher: Callable[[str], FetchedArticle] | None = None,
        page_reader: Callable[[str, str], SourceDocument] = read_official_page,
    ) -> None:
        self.entries = list(entries)
        self.link_fetcher = link_fetcher or PublicWebFetcher().fetch
        self.page_reader = page_reader

    def items_for(self, url: str, page_text: str, wanted_by_claim: dict[str, set[str]]) -> dict[str, list[EvidenceItem]]:
        """Evidence items per claim id; empty when the site is not trusted."""
        site = trusted_site(url, self.entries)
        if site is None:
            return {}
        subpages = self._subpage_links(url, site)
        fetched: dict[str, SourceDocument | None] = {}
        result: dict[str, list[EvidenceItem]] = {}
        for claim_id, wanted in wanted_by_claim.items():
            items: list[EvidenceItem] = []
            passages = best_passages(page_text, wanted)
            if passages:
                items.append(self._item(site, url, f"Podana strona ({site.publisher})", passages))
            ranked = sorted(
                ((score(label, wanted), link) for link, label in subpages), key=lambda pair: -pair[0]
            )
            for points, link in ranked[:MAX_SUBPAGES]:
                if points == 0:
                    break
                document = fetched.get(link) if link in fetched else self._read(link, site, fetched)
                if document is None:
                    continue
                sub_passages = best_passages(document.text, wanted)
                if sub_passages:
                    items.append(self._item(site, document.url, document.title, sub_passages))
            if items:
                result[claim_id] = items
        return result

    def _subpage_links(self, url: str, site: TrustedSite) -> list[tuple[str, str]]:
        try:
            article = self.link_fetcher(url)
            if article.content_type == "application/pdf":
                return []
            parsed = describe_html(article)
        except Exception:
            log.warning("Reading links of submitted page %s failed", url, exc_info=True)
            return []
        host = urlsplit(url).hostname
        return [
            (link.url, " ".join(link.labels))
            for link in parsed.subpages
            if urlsplit(link.url).hostname == host and link.url.rstrip("/") != url.rstrip("/") and link.labels
        ]

    def _read(self, link: str, site: TrustedSite, cache: dict[str, SourceDocument | None]) -> SourceDocument | None:
        try:
            cache[link] = self.page_reader(link, site.domain)
        except Exception:
            log.warning("Reading subpage %s failed", link, exc_info=True)
            cache[link] = None
        return cache[link]

    @staticmethod
    def _item(site: TrustedSite, url: str, title: str, snippet: str) -> EvidenceItem:
        return EvidenceItem(
            source="submitted_page",
            kind="article" if site.kind == "fact_checker" else "official",
            title=title or url,
            url=url,
            publisher=site.publisher,
            catalog_slug=site.slug,
            snippet=snippet,
            lang="pl",
        )
