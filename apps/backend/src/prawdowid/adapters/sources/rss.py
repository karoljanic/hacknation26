"""Wspólny parser RSS/Atom z paginowanym fallbackiem HTML (CERT Polska, Policja, inne feedy).

Obsługuje:
- bezpośrednie kanały RSS 2.0 i Atom (parsowane standardową biblioteką XML),
- automatyczne wykrywanie kanału RSS na podanej stronie HTML (w tym 1 poziom w głąb dla stron /rss),
- fallback na paginowaną listę linków HTML, jeśli serwis nie wystawia RSS (np. moje.cert.pl/komunikaty/),
- dociąganie pełnej treści artykułu z sanityzacją HTML (ochrona przed prompt injection).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import logging
import os
import re
import socket
import ssl
import time
from typing import Protocol
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit
import xml.etree.ElementTree as ET

import httpx

from prawdowid.adapters.sources.gov_pl import (
    RawResource,
    _CHROME_TAGS,
    content_hash,
    normalize_text,
    parse_gov_date,
    sanitize_soup,
    sort_latest,
)
from prawdowid.adapters.sources.web import _is_public_address

log = logging.getLogger(__name__)

DEFAULT_USER_AGENT = "Prawdowid/0.1 (rss corpus ingestion)"
_SKIP_LINK_FRAGMENTS = ("kontakt", "bip", "polityka-prywatnosci", "deklaracja", "szukaj")
_PATH_PAGE_RE = re.compile(r"/(?:page|strona)/(\d+)/?$", re.IGNORECASE)


class RssResourceFetcher(Protocol):
    def get(self, url: str) -> RawResource: ...


@dataclass(frozen=True)
class FeedItem:
    title: str
    url: str
    summary: str
    published_at: datetime | None


@dataclass(frozen=True)
class RssDocument:
    """Znormalizowany dokument z kanału RSS/Atom lub listingu HTML."""

    url: str
    domain: str
    title: str
    text: str
    content_hash: str
    fetched_at: datetime
    published_at: datetime | None = None
    source_feed_url: str | None = None
    lang: str = "pl"


class RssHttpFetcher:
    """Klient HTTP z weryfikacją publicznych adresów IP (SSRF), obsługą gzip i SSL_CERT_FILE."""

    def __init__(
        self,
        *,
        timeout: float = 15.0,
        max_bytes: int = 6_000_000,
        max_retries: int = 2,
        max_redirects: int = 4,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self.max_bytes = max_bytes
        self.max_retries = max_retries
        self.max_redirects = max_redirects
        verify: bool | ssl.SSLContext = True
        ca_bundle = os.environ.get("SSL_CERT_FILE")
        if ca_bundle:
            verify = ssl.create_default_context(cafile=ca_bundle)
        self._client = httpx.Client(
            timeout=httpx.Timeout(timeout),
            follow_redirects=False,
            verify=verify,
            headers={
                "User-Agent": user_agent,
                "Accept": (
                    "application/rss+xml,application/atom+xml,application/xml,"
                    "text/xml,text/html;q=0.9,*/*;q=0.5"
                ),
                "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.5",
            },
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> RssHttpFetcher:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def get(self, url: str) -> RawResource:
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                return self._get_with_redirects(url)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_exc = exc
                if attempt >= self.max_retries:
                    raise RuntimeError(f"Błąd sieci dla {url}: {exc}") from exc
                time.sleep(0.5 * (2**attempt))
            except _RetryableStatus as exc:
                last_exc = exc
                if attempt >= self.max_retries:
                    raise RuntimeError(str(exc)) from exc
                time.sleep(0.5 * (2**attempt))
        raise RuntimeError(f"Nie udało się pobrać {url}: {last_exc}")

    def _get_with_redirects(self, url: str) -> RawResource:
        current = url
        for hop in range(self.max_redirects + 1):
            _validate_public_url(current)
            with self._client.stream("GET", current) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location or hop == self.max_redirects:
                        raise RuntimeError(f"Przekroczono limit przekierowań dla {current}")
                    current = str(response.url.join(location))
                    continue
                if response.status_code in {429, 500, 502, 503, 504}:
                    raise _RetryableStatus(f"HTTP {response.status_code} dla {current}")
                if response.status_code != 200:
                    raise RuntimeError(f"HTTP {response.status_code} dla {current}")

                buf = bytearray()
                for chunk in response.iter_bytes():
                    buf.extend(chunk)
                    if len(buf) > self.max_bytes:
                        raise ValueError(f"Odpowiedź przekracza limit {self.max_bytes} B: {current}")

                ctype = response.headers.get("content-type", "").split(";")[0].strip().lower()
                return RawResource(
                    url=str(response.url),
                    content=bytes(buf),
                    content_type=ctype,
                    fetched_at=datetime.now(timezone.utc),
                    encoding=response.encoding,
                )
        raise RuntimeError(f"Przekroczono limit przekierowań dla {url}")


class _RetryableStatus(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# Czyste funkcje parsujące RSS / Atom / HTML (testowalne bez sieci)
# --------------------------------------------------------------------------- #


def _validate_public_url(url: str) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError(f"Nieobsługiwany adres URL: {url}")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if port not in {80, 443}:
        raise ValueError(f"Obsługiwane są wyłącznie porty 80 i 443: {url}")
    host = parsed.hostname.encode("idna").decode("ascii")
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not addresses or any(not _is_public_address(addr[4][0]) for addr in addresses):
        raise ValueError(f"Adres rozwiązuje się na niepubliczne IP: {url}")


def _domain(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _canonical_url(url: str) -> str:
    parts = urlsplit(url)
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


def _local_tag(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower() if "}" in tag else tag.lower()


def _child_text(elem: ET.Element, names: tuple[str, ...]) -> str:
    wanted = set(names)
    for child in elem:
        if _local_tag(child.tag) in wanted:
            raw = "".join(child.itertext()).strip()
            if raw:
                return raw
    return ""


def parse_feed_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = normalize_text(value)
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
        if dt is not None:
            return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, IndexError):
        pass
    return parse_gov_date(raw)


def _looks_like_xml_feed(content: bytes, content_type: str) -> bool:
    if any(tok in content_type for tok in ("rss", "atom", "xml")):
        return True
    head = content[:300].lstrip().lower()
    return head.startswith((b"<?xml", b"<rss", b"<feed", b"<rdf:rdf"))


def parse_feed_xml(xml_bytes: bytes | str, *, base_url: str = "") -> list[FeedItem]:
    """Parsuje kanał RSS 2.0 lub Atom do listy ``FeedItem``."""
    raw = xml_bytes.encode("utf-8") if isinstance(xml_bytes, str) else xml_bytes
    root = ET.fromstring(raw)
    items: list[FeedItem] = []

    for elem in root.iter():
        tag = _local_tag(elem.tag)
        if tag == "item":
            title = _child_text(elem, ("title",))
            link = _child_text(elem, ("link", "guid"))
            raw_body = _child_text(elem, ("encoded", "content", "description", "summary"))
            raw_date = _child_text(elem, ("pubdate", "date", "published", "updated"))
        elif tag == "entry":
            title = _child_text(elem, ("title",))
            link = ""
            for child in elem:
                if _local_tag(child.tag) == "link":
                    href = child.attrib.get("href", "").strip()
                    rel = child.attrib.get("rel", "alternate").strip().lower()
                    if href and rel in {"", "alternate"}:
                        link = href
                        break
            if not link:
                link = _child_text(elem, ("id",))
            raw_body = _child_text(elem, ("content", "summary"))
            raw_date = _child_text(elem, ("published", "updated"))
        else:
            continue

        if not title or not link:
            continue

        full_url = urljoin(base_url, link) if base_url else link
        if "<" in raw_body and ">" in raw_body:
            summary = normalize_text(sanitize_soup(raw_body).get_text(" ", strip=True))
        else:
            summary = normalize_text(raw_body)

        items.append(
            FeedItem(
                title=normalize_text(title),
                url=full_url,
                summary=summary,
                published_at=parse_feed_datetime(raw_date),
            )
        )

    return items


def discover_feed_urls(
    html: bytes | str,
    page_url: str,
    *,
    encoding: str | None = None,
    prefer_keywords: tuple[str, ...] = ("aktualno", "komunikat", "rss"),
) -> list[str]:
    """Wykrywa adresy RSS/Atom w nagłówku <link> lub odnośnikach na stronie."""
    soup = sanitize_soup(html, encoding=encoding)
    found: list[str] = []

    for link in soup.find_all("link"):
        rel = " ".join(link.get("rel") or []).lower()
        ltype = str(link.get("type") or "").lower()
        href = str(link.get("href") or "").strip()
        if href and ("rss" in ltype or "atom" in ltype or ("alternate" in rel and "xml" in ltype)):
            found.append(urljoin(page_url, href))

    for anchor in soup.find_all("a", href=True):
        href = str(anchor["href"]).strip()
        href_l = href.lower()
        label = normalize_text(anchor.get_text(" ", strip=True)).lower()
        if any(tok in href_l for tok in (".rss", "/rss", "atom", "feed.xml", "rss.xml")):
            full = urljoin(page_url, href)
            if any(kw in label or kw in href_l for kw in prefer_keywords):
                found.insert(0, full)
            else:
                found.append(full)

    deduped: list[str] = []
    seen: set[str] = set()
    for url in found:
        if url not in seen:
            seen.add(url)
            deduped.append(url)
    return deduped


def extract_article_html(
    html: bytes | str, *, encoding: str | None = None
) -> tuple[str | None, str, datetime | None]:
    """Wyciąga oczyszczony tytuł, treść i datę z podstrony artykułu."""
    soup = sanitize_soup(html, encoding=encoding)

    published_at: datetime | None = None
    for attrs in (
        {"property": "article:published_time"},
        {"name": "date"},
        {"itemprop": "datePublished"},
    ):
        meta = soup.find("meta", attrs=attrs)
        if meta and meta.get("content"):
            published_at = parse_feed_datetime(str(meta["content"]))
            if published_at is not None:
                break
    if published_at is None:
        for selector in ("article time", "time", ".date", ".data", ".event-date"):
            node = soup.select_one(selector)
            if node is not None:
                published_at = parse_feed_datetime(
                    str(node.get("datetime") or node.get_text(" ", strip=True))
                )
                if published_at is not None:
                    break

    title: str | None = None
    for selector in ("article h1", "main h1", "h1", "article h2"):
        node = soup.select_one(selector)
        if node is not None:
            cand = normalize_text(node.get_text(" ", strip=True))
            if cand:
                title = cand
                break
    if not title and soup.title:
        title = normalize_text(soup.title.get_text(" ", strip=True))

    for chrome in soup.find_all(_CHROME_TAGS):
        chrome.decompose()

    root = soup.find("article") or soup.find("main") or soup.body or soup
    blocks: list[str] = []
    seen: set[str] = set()
    nested = ["h1", "h2", "h3", "h4", "p", "li", "blockquote"]
    for block in root.find_all(("h1", "h2", "h3", "h4", "p", "li", "blockquote", "tr")):
        if block.name != "tr" and block.find(nested) is not None:
            continue
        txt = normalize_text(block.get_text(" ", strip=True))
        if txt and txt not in seen:
            seen.add(txt)
            blocks.append(txt)

    text = "\n".join(blocks) if blocks else normalize_text(root.get_text("\n", strip=True))
    return title, text, published_at


def extract_html_listing_links(
    html: bytes | str,
    listing_url: str,
    *,
    encoding: str | None = None,
    limit: int | None = 10,
) -> list[str]:
    """Fallback HTML: wyciąga linki do najnowszych wpisów w obrębie tej samej domeny."""
    soup = sanitize_soup(html, encoding=encoding)
    for chrome in soup.find_all(_CHROME_TAGS):
        chrome.decompose()

    base_domain = _domain(listing_url)
    base_canonical = _canonical_url(listing_url)
    base_path = urlsplit(base_canonical).path.rstrip("/")
    root = soup.find("main") or soup.find("article") or soup.body or soup

    links: list[str] = []
    seen: set[str] = {base_canonical}

    for anchor in root.find_all("a", href=True):
        href = str(anchor["href"]).strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        full = _canonical_url(urljoin(listing_url, href))
        parts = urlsplit(full)
        if _domain(full) != base_domain:
            continue
        if "page=" in parts.query.lower():
            continue
        path_lower = parts.path.lower()
        if any(skip in path_lower for skip in _SKIP_LINK_FRAGMENTS) or "/rss" in path_lower:
            continue
        if base_path and base_path != "/" and not path_lower.startswith(base_path.lower() + "/"):
            if len([s for s in parts.path.split("/") if s]) < 2:
                continue
        if full in seen:
            continue
        label = normalize_text(anchor.get_text(" ", strip=True))
        if len(label) < 12:
            continue

        seen.add(full)
        links.append(full)
        if limit is not None and len(links) >= limit:
            break

    return links


def _html_page_number(url: str) -> int:
    parts = urlsplit(url)
    qs = parse_qs(parts.query)
    for key in ("page", "strona", "p"):
        if key in qs and qs[key]:
            try:
                return max(1, int(qs[key][0]))
            except ValueError:
                pass
    match = _PATH_PAGE_RE.search(parts.path)
    if match:
        return max(1, int(match.group(1)))
    return 1


def extract_next_page_url(
    html: bytes | str,
    current_url: str,
    *,
    encoding: str | None = None,
) -> str | None:
    """Wykrywa link do kolejnej strony paginacji na stronach HTML (rel=next, ?page=N, /page/N)."""
    soup = sanitize_soup(html, encoding=encoding)
    base_domain = _domain(current_url)
    current_canonical = _canonical_url(current_url)
    current_page = _html_page_number(current_url)
    target_page = current_page + 1

    def _valid(href: str) -> str | None:
        full = _canonical_url(urljoin(current_url, href.strip()))
        if _domain(full) != base_domain or full == current_canonical:
            return None
        return full

    rel_next = soup.find(
        "a",
        rel=lambda r: bool(
            r and "next" in [x.lower() for x in (r if isinstance(r, list) else [r])]
        ),
    )
    if rel_next and rel_next.get("href"):
        candidate = _valid(str(rel_next["href"]))
        if candidate:
            return candidate

    for anchor in soup.find_all("a", href=True):
        href = str(anchor["href"]).strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        candidate = _valid(href)
        if not candidate:
            continue
        cand_page = _html_page_number(candidate)
        if cand_page == target_page:
            return candidate
        label = normalize_text(anchor.get_text(" ", strip=True)).lower()
        aria = str(anchor.get("aria-label") or "").lower()
        if (
            any(tok in label or tok in aria for tok in ("następna", "nastepna", "next", "»"))
            and cand_page > current_page
        ):
            return candidate

    return None


class RssParser:
    """Uniwersalny parser RSS/Atom z automatycznym wykrywaniem feedu i paginacją fallbacku HTML."""

    def __init__(
        self,
        fetcher: RssResourceFetcher,
        *,
        fetch_full_article: bool = True,
        min_summary_length: int = 280,
        min_text_length: int = 40,
        request_delay: float = 0.0,
    ) -> None:
        self.fetcher = fetcher
        self.fetch_full_article = fetch_full_article
        self.min_summary_length = min_summary_length
        self.min_text_length = min_text_length
        self.request_delay = max(0.0, request_delay)

    def fetch_latest(
        self,
        url: str,
        *,
        limit: int = 10,
        max_pages: int = 10,
    ) -> list[RssDocument]:
        """Pobiera do ``limit`` najnowszych dokumentów z RSS/Atom albo z paginowanego listingu HTML."""
        if limit < 0:
            raise ValueError("limit nie może być ujemny")
        if limit == 0:
            return []

        res = self.fetcher.get(url)
        if _looks_like_xml_feed(res.content, res.content_type):
            return self._from_feed_resource(res, limit=limit)

        # Próba automatycznego wykrycia RSS (z 1 przeskokiem dla stron typu /pol/rss).
        for candidate_feed in discover_feed_urls(res.content, res.url, encoding=res.encoding)[:3]:
            try:
                feed_res = self.fetcher.get(candidate_feed)
                if _looks_like_xml_feed(feed_res.content, feed_res.content_type):
                    docs = self._from_feed_resource(feed_res, limit=limit)
                    if docs:
                        return docs
                else:
                    for nested in discover_feed_urls(
                        feed_res.content, feed_res.url, encoding=feed_res.encoding
                    )[:2]:
                        nested_res = self.fetcher.get(nested)
                        if _looks_like_xml_feed(nested_res.content, nested_res.content_type):
                            docs = self._from_feed_resource(nested_res, limit=limit)
                            if docs:
                                return docs
            except Exception as exc:
                log.warning("RSS: nie udało się sprawdzić kandydata feedu %s: %s", candidate_feed, exc)
                continue

        return self._from_html_listing(res, limit=limit, max_pages=max_pages)

    def fetch_source(self, url: str, *, limit: int = 10, max_pages: int = 10) -> list[RssDocument]:
        """Alias zachowany dla kompatybilności wstecznej."""
        return self.fetch_latest(url, limit=limit, max_pages=max_pages)

    def _from_feed_resource(self, feed_res: RawResource, *, limit: int) -> list[RssDocument]:
        items = sort_latest(parse_feed_xml(feed_res.content, base_url=feed_res.url))
        docs: list[RssDocument] = []
        seen_urls: set[str] = set()
        seen_hashes: set[str] = set()

        for item in items:
            canonical = _canonical_url(item.url)
            if canonical in seen_urls:
                continue
            seen_urls.add(canonical)

            text = item.summary
            title = item.title
            published_at = item.published_at
            final_url = item.url
            fetched_at = feed_res.fetched_at

            if self.fetch_full_article and len(text) < self.min_summary_length:
                if self.request_delay > 0:
                    time.sleep(self.request_delay)
                try:
                    art = self.fetcher.get(item.url)
                    art_title, art_text, art_pub = extract_article_html(
                        art.content, encoding=art.encoding
                    )
                    if len(art_text) > len(text):
                        text = art_text
                        final_url = art.url
                        fetched_at = art.fetched_at
                    if not title and art_title:
                        title = art_title
                    if published_at is None and art_pub is not None:
                        published_at = art_pub
                except Exception as exc:
                    log.warning("RSS: nie udało się dociągnąć artykułu %s: %s", item.url, exc)

            combined = normalize_text(f"{title}\n\n{text}" if text else title)
            if len(combined) < self.min_text_length:
                continue

            doc_hash = content_hash(combined)
            if doc_hash in seen_hashes:
                continue
            seen_hashes.add(doc_hash)

            docs.append(
                RssDocument(
                    url=final_url,
                    domain=_domain(final_url),
                    title=title,
                    text=combined,
                    content_hash=doc_hash,
                    fetched_at=fetched_at,
                    published_at=published_at,
                    source_feed_url=feed_res.url,
                )
            )
            if len(docs) >= limit:
                break

        return sort_latest(docs, limit=limit)

    def _from_html_listing(
        self,
        first_res: RawResource,
        *,
        limit: int,
        max_pages: int,
    ) -> list[RssDocument]:
        docs: list[RssDocument] = []
        seen_articles: set[str] = set()
        seen_hashes: set[str] = set()
        seen_pages: set[str] = set()
        current_res: RawResource | None = first_res

        for _ in range(max(1, max_pages)):
            if current_res is None:
                break
            canonical_page = _canonical_url(current_res.url)
            if canonical_page in seen_pages:
                break
            seen_pages.add(canonical_page)

            links = extract_html_listing_links(
                current_res.content,
                current_res.url,
                encoding=current_res.encoding,
                limit=None,
            )
            new_links = [link for link in links if link not in seen_articles]
            if not new_links:
                break

            for article_url in new_links:
                seen_articles.add(article_url)
                if self.request_delay > 0:
                    time.sleep(self.request_delay)
                try:
                    art = self.fetcher.get(article_url)
                    title, text, published_at = extract_article_html(
                        art.content, encoding=art.encoding
                    )
                    if len(text) < self.min_text_length:
                        continue
                    doc_hash = content_hash(text)
                    if doc_hash in seen_hashes:
                        continue
                    seen_hashes.add(doc_hash)
                    docs.append(
                        RssDocument(
                            url=art.url,
                            domain=_domain(art.url),
                            title=title or art.url,
                            text=text,
                            content_hash=doc_hash,
                            fetched_at=art.fetched_at,
                            published_at=published_at,
                            source_feed_url=first_res.url,
                        )
                    )
                    if len(docs) >= limit:
                        break
                except Exception as exc:
                    log.warning("RSS(HTML): pominięto artykuł %s: %s", article_url, exc)
                    continue

            if len(docs) >= limit:
                break

            next_url = extract_next_page_url(
                current_res.content,
                current_res.url,
                encoding=current_res.encoding,
            )
            if next_url is None:
                break
            try:
                current_res = self.fetcher.get(next_url)
            except Exception as exc:
                log.warning("RSS(HTML): błąd pobierania strony paginacji %s: %s", next_url, exc)
                break

        return sort_latest(docs, limit=limit)
