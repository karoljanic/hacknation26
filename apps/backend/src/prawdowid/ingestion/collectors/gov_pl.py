from collections.abc import Iterator, Mapping
from typing import Any

from prawdowid.adapters.sources.gov_pl import GovPlDocument, GovPlParser
from prawdowid.ingestion.domain import CorpusDocument, Skipped


class GovPlCollector:
    """params: ``listing_urls`` (list), ``static_urls`` (list), ``limit`` per listing."""

    def __init__(self, parser: GovPlParser) -> None:
        self.parser = parser

    def collect(self, params: Mapping[str, Any]) -> Iterator[CorpusDocument | Skipped]:
        limit = int(params.get("limit", 8))
        for url in params.get("listing_urls", []):
            # A broken section page should not stop the other sections.
            try:
                docs = self.parser.fetch_listing(url, limit=limit)
            except Exception as exc:
                yield Skipped(reason=f"listing failed: {exc}", ref=url)
                continue
            if not docs:
                yield Skipped(reason="listing returned no documents", ref=url)
            for doc in docs:
                yield to_corpus(doc)
        for url in params.get("static_urls", []):
            try:
                doc = self.parser.fetch_page(url)
            except Exception as exc:
                yield Skipped(reason=f"page failed: {exc}", ref=url)
                continue
            yield to_corpus(doc) if doc is not None else Skipped(reason="page has no text", ref=url)


def to_corpus(doc: GovPlDocument) -> CorpusDocument:
    metadata: dict[str, Any] = {}
    if doc.department:
        metadata["department"] = doc.department
    if doc.attachments:
        metadata["attachments"] = [{"url": a.url, "title": a.title} for a in doc.attachments]
    if doc.errors:
        metadata["parse_errors"] = list(doc.errors)
    return CorpusDocument(
        url=doc.url,
        title=doc.title,
        text=doc.text,
        fetched_at=doc.fetched_at,
        published_at=doc.published_at,
        lang=doc.lang,
        publisher_site="gov.pl",
        metadata=metadata,
    )
