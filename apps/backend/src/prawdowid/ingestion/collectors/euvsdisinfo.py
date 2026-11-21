"""EUvsDisinfo cases/articles via the MCP adapter.

Record shapes observed from the live server (2026-11):

* cases:    id, title, claim (HTML), fact (HTML), date (unix seconds),
            keywords/countries [{_id, title}], linksToDisinformation, url
* articles: id, title, content (HTML), dateGmt ("YYYY-MM-DD HH:MM:SS", GMT),
            keywords/categories [{slug, name, ...}], articleLanguage [{slug}], url

Records are read from ``structured_content`` or JSON text blocks. HTML is
converted to text with the same sanitiser as gov.pl (scripts, comments and
CSS-hidden elements are dropped). A record without a URL or text is skipped
and counted - nothing is guessed.
"""

import asyncio
import json
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError

from prawdowid.adapters.sources.gov_pl import normalize_text, sanitize_soup
from prawdowid.domain.euvsdisinfo import EUvsDisinfoPageRequest, EUvsDisinfoResult
from prawdowid.ingestion.domain import CorpusDocument, Skipped

URL_KEYS = ("url", "link", "permalink")
TITLE_KEYS = ("title", "name", "headline")
CLAIM_KEYS = ("claim", "summary")
FACT_KEYS = ("fact", "disproof")
BODY_KEYS = ("content", "text", "body")
DATE_KEYS = ("date", "dateGmt", "published")
TERM_LIST_KEYS = ("keywords", "countries", "categories")
MAX_LINKED_HEADLINES = 10
_ISO_LANGUAGE = {"ua": "uk"}  # EUvsDisinfo slugs -> ISO 639-1 where they differ
RECORD_LIST_KEYS = ("cases", "articles", "items", "results", "data", "records")
MAX_FAILURES_IN_A_ROW = 3

# Opens the adapter session; returns an async context manager yielding an
# object with async ``cases(req)`` / ``articles(req)`` methods.
SessionFactory = Callable[[], AbstractAsyncContextManager[Any]]


class EUvsDisinfoCollector:
    """params: ``tools`` (["cases", "articles"]), ``page_size`` (≤100), ``max_records`` per tool."""

    def __init__(self, open_session: SessionFactory) -> None:
        self.open_session = open_session

    def collect(self, params: Mapping[str, Any]) -> Iterator[CorpusDocument | Skipped]:
        # Fetch everything in one event loop, then map synchronously.
        for item in asyncio.run(self._fetch(params)):
            if isinstance(item, Skipped):
                yield item
                continue
            tool, result = item
            for record in extract_records(result):
                yield to_corpus(record, tool=tool, fetched_at=result.fetched_at)

    async def _fetch(self, params: Mapping[str, Any]) -> list[tuple[str, EUvsDisinfoResult] | Skipped]:
        tools = list(params.get("tools", ["cases", "articles"]))
        for tool in tools:
            if tool not in ("cases", "articles"):
                raise ValueError(f"Unsupported EUvsDisinfo tool for ingestion: {tool}")
        max_records = int(params.get("max_records", 100))
        out: list[tuple[str, EUvsDisinfoResult] | Skipped] = []
        async with self.open_session() as source:
            for tool in tools:
                page_size = int(params.get("page_size", 50))
                offset = 0
                failures_in_a_row = 0
                while offset < max_records:
                    limit = min(page_size, max_records - offset)
                    try:
                        result = await getattr(source, tool)(
                            EUvsDisinfoPageRequest(offset=offset, limit=limit)
                        )
                    except Exception as exc:
                        # Typically the adapter's 2 MB result cap on long articles:
                        # retry the same offset with smaller pages, down to 1 record.
                        if limit > 1:
                            page_size = max(1, limit // 2)
                            continue
                        out.append(Skipped(reason=f"{tool} record failed: {exc}", ref=f"{tool}@{offset}"))
                        failures_in_a_row += 1
                        if failures_in_a_row >= MAX_FAILURES_IN_A_ROW:
                            out.append(Skipped(reason=f"{tool}: giving up after repeated failures",
                                               ref=f"{tool}@{offset}"))
                            break
                        offset += 1
                        continue
                    failures_in_a_row = 0
                    out.append((tool, result))
                    if len(extract_records(result)) < limit:
                        break
                    offset += limit
        return out


def extract_records(result: EUvsDisinfoResult) -> list[dict[str, Any]]:
    candidates: list[Any] = []
    if result.structured_content is not None:
        candidates.append(result.structured_content)
    for block in result.content:
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            try:
                candidates.append(json.loads(block["text"]))
            except json.JSONDecodeError:
                continue
    for candidate in candidates:
        records = _as_record_list(candidate)
        if records:
            return records
    return []


def _as_record_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        for key in RECORD_LIST_KEYS:
            if isinstance(value.get(key), list):
                return [item for item in value[key] if isinstance(item, dict)]
    return []


_BLOCK_TAGS = ("p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "tr", "ul", "ol", "table")


def html_to_text(value: str) -> str:
    """Sanitised plain text: line breaks after block elements, inline text kept together."""
    if "<" not in value:
        return normalize_text(value)
    soup = sanitize_soup(value)
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for block in soup.find_all(_BLOCK_TAGS):
        block.insert_after("\n")
    return normalize_text(soup.get_text())


def _first(record: Mapping[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _first_text(record: Mapping[str, Any], keys: tuple[str, ...]) -> str | None:
    raw = _first(record, keys)
    text = html_to_text(raw) if raw else ""
    return text or None


def parse_date(value: Any) -> datetime | None:
    """Unix seconds (cases) or 'YYYY-MM-DD[ HH:MM:SS]' in GMT (articles) -> UTC."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _term_names(value: Any) -> list[str]:
    """keywords/countries: [{'title': ...}] (cases) or [{'name': ...}] (articles) or [str]."""
    names: list[str] = []
    if isinstance(value, list):
        for item in value:
            name = item if isinstance(item, str) else (
                item.get("title") or item.get("name") if isinstance(item, dict) else None
            )
            if isinstance(name, str) and name.strip():
                names.append(name.strip())
    return names


def _language(record: Mapping[str, Any], tool: str) -> str:
    languages = record.get("articleLanguage")
    if isinstance(languages, list) and languages and isinstance(languages[0], dict):
        slug = languages[0].get("slug")
        if isinstance(slug, str) and 2 <= len(slug) <= 8:
            slug = slug.lower()
            return _ISO_LANGUAGE.get(slug, slug)
    return "en"  # case summaries are written in English


def to_corpus(record: Mapping[str, Any], *, tool: str, fetched_at: datetime) -> CorpusDocument | Skipped:
    ref = str(record.get("id")) if record.get("id") is not None else None
    url = _first(record, URL_KEYS)
    if url is None or not url.startswith(("http://", "https://")):
        return Skipped(reason=f"{tool} record without URL", ref=ref)
    claim, fact = _first_text(record, CLAIM_KEYS), _first_text(record, FACT_KEYS)
    body = _first_text(record, BODY_KEYS)
    parts = []
    if claim:
        parts.append(f"Teza (dezinformacja): {claim}" if tool == "cases" else claim)
    if fact:
        parts.append(f"Analiza EUvsDisinfo: {fact}")
    if body and body not in (claim, fact):
        parts.append(body)
    if not parts:
        return Skipped(reason=f"{tool} record without text", ref=url)
    title = html_to_text(_first(record, TITLE_KEYS) or "") or (claim or parts[0])[:200]
    published_at = next(
        (d for d in (parse_date(record.get(key)) for key in DATE_KEYS) if d is not None), None
    )
    metadata: dict[str, Any] = {"via": "euvsdisinfo", "record_type": tool}
    if ref:
        metadata["record_id"] = ref
    for key in TERM_LIST_KEYS:
        names = _term_names(record.get(key))
        if names:
            metadata[key] = names
    links = record.get("linksToDisinformation")
    if isinstance(links, list):
        headlines = [
            link["headLine"] for link in links
            if isinstance(link, dict) and isinstance(link.get("headLine"), str)
        ]
        metadata["disinformation_links"] = len(links)
        if headlines:
            metadata["disinformation_headlines"] = headlines[:MAX_LINKED_HEADLINES]
    try:
        return CorpusDocument(
            url=url,
            title=title,
            text="\n".join(parts),
            fetched_at=fetched_at if fetched_at.tzinfo else fetched_at.replace(tzinfo=timezone.utc),
            published_at=published_at,
            lang=_language(record, tool),
            publisher_site="euvsdisinfo.eu",
            metadata=metadata,
        )
    except ValidationError as exc:
        return Skipped(reason=f"invalid {tool} record: {exc.error_count()} errors", ref=url)
