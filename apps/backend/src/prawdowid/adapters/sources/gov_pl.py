"""Wspólny parser i konektor dla stron CMS gov.pl (www.gov.pl/web/...).

Obsługuje:
- strony listujące (np. /aktualnosci, /ostrzezenia, /komunikaty-nawigacyjne),
- pojedyncze strony informacyjne (np. /prawa-pasazerow, /19-111),
- załączniki PDF pod /attachment/... (np. Raport Dobowy RCB, Wody Polskie).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import io
import os
import re
import ssl
import time
import unicodedata
from typing import Protocol
from urllib.parse import urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup, Comment, Tag
import httpx

try:
    from pypdf import PdfReader
except ImportError:  # pragma: no cover - opcjonalna zależność do PDF
    PdfReader = None  # type: ignore[assignment]

WARSAW_TZ = ZoneInfo("Europe/Warsaw")
ALLOWED_HOSTS = frozenset({"gov.pl", "www.gov.pl"})
DEFAULT_USER_AGENT = "Prawdowid/0.1 (gov.pl corpus ingestion)"

_UNTRUSTED_TAGS = ("script", "style", "noscript", "iframe", "svg", "object", "embed", "template")
_CHROME_TAGS = ("nav", "header", "footer", "aside", "form")
_HIDDEN_CLASSES = frozenset({"d-none", "hidden", "sr-only", "visually-hidden", "screen-reader"})
_HIDDEN_STYLE_RE = re.compile(
    r"(?:display\s*:\s*none|visibility\s*:\s*hidden|"
    r"opacity\s*:\s*0(?:\.0+)?(?:\s|;|$)|font-size\s*:\s*0(?:px|em|rem|%)?(?:\s|;|$))",
    re.IGNORECASE,
)

_DMY_RE = re.compile(
    r"\b(?P<day>\d{1,2})\.(?P<month>\d{1,2})\.(?P<year>20\d{2})"
    r"(?:[\s,T]+(?P<hour>\d{1,2}):(?P<minute>\d{2})(?::(?P<second>\d{2}))?)?\b"
)
_YMD_RE = re.compile(
    r"\b(?P<year>20\d{2})-(?P<month>\d{1,2})-(?P<day>\d{1,2})"
    r"(?:[\sT]+(?P<hour>\d{1,2}):(?P<minute>\d{2})(?::(?P<second>\d{2}))?)?\b"
)

_SKIP_PATH_SEGMENTS = frozenset(
    {
        "kontakt",
        "deklaracja-dostepnosci",
        "polityka-prywatnosci",
        "mapa-strony",
        "bip",
        "wyszukiwarka",
        "szukaj",
    }
)


@dataclass(frozen=True)
class RawResource:
    url: str
    content: bytes
    content_type: str
    fetched_at: datetime
    encoding: str | None = None


class GovResourceFetcher(Protocol):
    def get(self, url: str) -> RawResource: ...


@dataclass(frozen=True)
class GovAttachment:
    url: str
    title: str
    text: str | None = None


@dataclass(frozen=True)
class GovPlDocument:
    """Znormalizowany dokument gotowy do zapisu w tabeli ``document`` i podziału na ``passage``."""

    url: str
    title: str
    text: str
    content_hash: str
    fetched_at: datetime
    published_at: datetime | None = None
    lang: str = "pl"
    department: str | None = None
    attachments: tuple[GovAttachment, ...] = ()
    errors: tuple[str, ...] = field(default_factory=tuple)


class GovPlHttpFetcher:
    """Lekki klient HTTP dla gov.pl (obsługuje gzip, PDF, retry i SSL_CERT_FILE)."""

    def __init__(
        self,
        *,
        timeout: float = 15.0,
        max_bytes: int = 8_000_000,
        max_retries: int = 2,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self.max_bytes = max_bytes
        self.max_retries = max_retries
        verify: bool | ssl.SSLContext = True
        ca_bundle = os.environ.get("SSL_CERT_FILE")
        if ca_bundle:
            verify = ssl.create_default_context(cafile=ca_bundle)
        self._client = httpx.Client(
            timeout=httpx.Timeout(timeout),
            follow_redirects=True,
            max_redirects=4,
            verify=verify,
            headers={
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.5",
                "Accept-Language": "pl-PL,pl;q=0.9",
            },
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> GovPlHttpFetcher:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def get(self, url: str) -> RawResource:
        _ensure_gov_pl_url(url)
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                with self._client.stream("GET", url) as response:
                    final_url = str(response.url)
                    _ensure_gov_pl_url(final_url)
                    if response.status_code in {429, 500, 502, 503, 504} and attempt < self.max_retries:
                        time.sleep(0.5 * (2**attempt))
                        continue
                    if response.status_code != 200:
                        raise RuntimeError(f"gov.pl HTTP {response.status_code} dla {final_url}")

                    buf = bytearray()
                    for chunk in response.iter_bytes():
                        buf.extend(chunk)
                        if len(buf) > self.max_bytes:
                            raise ValueError(
                                f"Zasób przekracza limit {self.max_bytes} B: {final_url}"
                            )

                    ctype = response.headers.get("content-type", "").split(";")[0].strip().lower()
                    return RawResource(
                        url=final_url,
                        content=bytes(buf),
                        content_type=ctype,
                        fetched_at=datetime.now(timezone.utc),
                        encoding=response.encoding,
                    )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_exc = exc
                if attempt >= self.max_retries:
                    raise RuntimeError(f"Błąd sieci przy pobieraniu {url}: {exc}") from exc
                time.sleep(0.5 * (2**attempt))

        raise RuntimeError(f"Nie udało się pobrać {url}: {last_exc}")


# --------------------------------------------------------------------------- #
# Czyste funkcje parsujące (testowalne bez zapytań sieciowych)
# --------------------------------------------------------------------------- #


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFC", text)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", text)
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def content_hash(text: str) -> str:
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


def _ensure_gov_pl_url(url: str) -> None:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or host not in ALLOWED_HOSTS:
        raise ValueError(f"Obsługiwane są wyłącznie adresy www.gov.pl / gov.pl: {url}")


def _canonical_url(url: str) -> str:
    parts = urlsplit(url)
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


def _extract_department(url: str) -> str | None:
    parts = [p for p in urlsplit(url).path.split("/") if p]
    if len(parts) >= 2 and parts[0] == "web":
        return parts[1].lower()
    return None


def _is_hidden_tag(tag: Tag) -> bool:
    if not isinstance(tag.attrs, dict):
        return False
    if tag.has_attr("hidden") or str(tag.get("aria-hidden", "")).strip().lower() == "true":
        return True
    style = str(tag.get("style", ""))
    if style and _HIDDEN_STYLE_RE.search(style):
        return True
    classes = tag.get("class") or []
    if isinstance(classes, str):
        classes = classes.split()
    return bool({str(c).lower() for c in classes} & _HIDDEN_CLASSES)


def sanitize_soup(html: bytes | str, *, encoding: str | None = None) -> BeautifulSoup:
    """Usuwa skrypty, komentarze HTML i elementy ukryte w CSS (ochrona przed prompt injection)."""
    soup = (
        BeautifulSoup(html, "html.parser", from_encoding=encoding)
        if isinstance(html, bytes)
        else BeautifulSoup(html, "html.parser")
    )
    for comment in soup.find_all(string=lambda node: isinstance(node, Comment)):
        comment.extract()
    for node in soup.find_all(_UNTRUSTED_TAGS):
        node.decompose()
    hidden = [
        node for node in soup.find_all(True) if isinstance(node, Tag) and _is_hidden_tag(node)
    ]
    for node in hidden:
        if node.parent is not None:
            node.decompose()
    return soup


def parse_gov_date(value: str | None) -> datetime | None:
    """Parsuje daty z gov.pl (np. '21.11.2026', ISO-8601) do UTC."""
    if not value:
        return None
    raw = normalize_text(value)
    if not raw:
        return None

    iso = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        dt = datetime.fromisoformat(iso)
        return (dt.replace(tzinfo=WARSAW_TZ) if dt.tzinfo is None else dt).astimezone(timezone.utc)
    except ValueError:
        pass

    for pattern in (_DMY_RE, _YMD_RE):
        match = pattern.search(raw)
        if match:
            try:
                dt = datetime(
                    int(match.group("year")),
                    int(match.group("month")),
                    int(match.group("day")),
                    int(match.group("hour") or 0),
                    int(match.group("minute") or 0),
                    int(match.group("second") or 0),
                    tzinfo=WARSAW_TZ,
                )
                return dt.astimezone(timezone.utc)
            except ValueError:
                pass
    return None


def extract_listing_links(
    html: bytes | str,
    listing_url: str,
    *,
    encoding: str | None = None,
    limit: int = 10,
) -> list[str]:
    """Wyciąga linki do najnowszych artykułów z listującej podstrony gov.pl."""
    soup = sanitize_soup(html, encoding=encoding)
    for chrome in soup.find_all(_CHROME_TAGS):
        chrome.decompose()

    base_canonical = _canonical_url(listing_url)
    department = _extract_department(listing_url)
    dept_prefix = f"/web/{department}/" if department else "/web/"

    # Priorytet: typowe kontenery list artykułów na gov.pl; fallback: całe <main>.
    containers = (
        soup.select(".art-prev, .article-area__list, article ul, main .list")
        or [soup.find("main") or soup.body or soup]
    )

    found: list[str] = []
    seen: set[str] = {base_canonical}

    for container in containers:
        for anchor in container.find_all("a", href=True):
            href = str(anchor["href"]).strip()
            if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
                continue
            full_url = _canonical_url(urljoin(listing_url, href))
            parts = urlsplit(full_url)
            if (parts.hostname or "").lower() not in ALLOWED_HOSTS:
                continue
            if not parts.path.lower().startswith(dept_prefix):
                continue
            if "page=" in parts.query.lower():
                continue
            last_segment = parts.path.rstrip("/").rsplit("/", 1)[-1].lower()
            if last_segment in _SKIP_PATH_SEGMENTS:
                continue
            if full_url in seen:
                continue

            seen.add(full_url)
            found.append(full_url)
            if len(found) >= limit:
                return found

    return found


def extract_attachment_links(
    soup: BeautifulSoup, page_url: str, *, max_attachments: int = 3
) -> list[tuple[str, str]]:
    """Znajduje załączniki PDF publikowane pod /attachment/... (np. raport dobowy RCB)."""
    results: list[tuple[str, str]] = []
    seen: set[str] = set()
    root = soup.find("article") or soup.find("main") or soup

    for anchor in root.find_all("a", href=True):
        href = str(anchor["href"]).strip()
        if not href:
            continue
        full_url = urljoin(page_url, href)
        parts = urlsplit(full_url)
        if (parts.hostname or "").lower() not in ALLOWED_HOSTS:
            continue

        is_gov_attachment = parts.path.lower().startswith("/attachment/")
        is_pdf_url = parts.path.lower().endswith(".pdf")
        label = normalize_text(anchor.get_text(" ", strip=True))
        classes = " ".join(anchor.get("class") or []).lower()
        looks_like_pdf = (
            is_pdf_url
            or "pdf" in label.lower()
            or "file-download" in classes
        )

        if (is_gov_attachment and looks_like_pdf) or is_pdf_url:
            if full_url not in seen:
                seen.add(full_url)
                results.append((full_url, label or "Załącznik PDF"))
                if len(results) >= max_attachments:
                    break
    return results


def extract_pdf_text(pdf_bytes: bytes) -> str:
    if PdfReader is None:
        raise RuntimeError("Brak pakietu pypdf — zainstaluj `pip install pypdf`, aby czytać PDF.")
    reader = PdfReader(io.BytesIO(pdf_bytes))
    pages = [normalize_text(page.extract_text() or "") for page in reader.pages]
    return "\n\n".join(page for page in pages if page)


def parse_gov_article_html(
    html: bytes | str,
    *,
    url: str,
    encoding: str | None = None,
) -> tuple[str, str, datetime | None, list[tuple[str, str]]]:
    """Parsuje pojedynczą stronę gov.pl. Zwraca (tytuł, tekst, published_at, pdf_links)."""
    soup = sanitize_soup(html, encoding=encoding)

    # 1. Data publikacji (gov.pl najczęściej używa .event-date lub meta article:published_time)
    published_at: datetime | None = None
    for meta_attrs in (
        {"property": "article:published_time"},
        {"name": "date"},
        {"itemprop": "datePublished"},
    ):
        meta = soup.find("meta", attrs=meta_attrs)
        if meta and meta.get("content"):
            published_at = parse_gov_date(str(meta["content"]))
            if published_at is not None:
                break
    if published_at is None:
        for selector in ("article .event-date", ".event-date", "article time", "time", ".date"):
            node = soup.select_one(selector)
            if node is not None:
                raw_dt = node.get("datetime") or node.get_text(" ", strip=True)
                published_at = parse_gov_date(str(raw_dt))
                if published_at is not None:
                    break

    # 2. Tytuł (na stronach artykułów gov.pl tytuł właściwy często jest w article h2)
    title: str | None = None
    for selector in (".article-area__article h2", "article h2", "article h1", "main h1"):
        heading = soup.select_one(selector)
        if heading is not None:
            candidate = normalize_text(heading.get_text(" ", strip=True))
            if candidate:
                title = candidate
                break
    if not title:
        og_title = soup.find("meta", attrs={"property": "og:title"})
        if og_title and og_title.get("content"):
            title = normalize_text(str(og_title["content"]))
    if not title and soup.title:
        title = normalize_text(soup.title.get_text(" ", strip=True))
    title = title or url

    # 3. Załączniki PDF zanim wytniemy elementy pomocnicze
    pdf_links = extract_attachment_links(soup, url)

    # 4. Treść właściwa (.intro + .editor-content to standard CMS gov.pl)
    for chrome in soup.find_all(_CHROME_TAGS):
        chrome.decompose()

    parts: list[str] = []
    seen_blocks: set[str] = set()

    def _add_block(raw: str) -> None:
        cleaned = normalize_text(raw)
        if cleaned and cleaned not in seen_blocks:
            seen_blocks.add(cleaned)
            parts.append(cleaned)

    intro = soup.select_one("article .intro, .article-area__article .intro, p.intro")
    if intro is not None:
        _add_block(intro.get_text(" ", strip=True))

    content_roots = soup.select(".editor-content")
    if not content_roots:
        fallback = soup.find("article") or soup.find("main") or soup.body or soup
        content_roots = [fallback]

    block_tags = ("h2", "h3", "h4", "p", "li", "blockquote", "tr")
    nested_blocks = ["h2", "h3", "h4", "p", "li", "blockquote"]
    for root in content_roots:
        for block in root.find_all(block_tags):
            if block.name != "tr" and block.find(nested_blocks) is not None:
                continue
            _add_block(block.get_text(" ", strip=True))

    text = "\n".join(parts)
    if not text and content_roots:
        text = normalize_text(" ".join(r.get_text(" ", strip=True) for r in content_roots))

    return title, text, published_at, pdf_links


# --------------------------------------------------------------------------- #
# Główny parser gov.pl
# --------------------------------------------------------------------------- #


class GovPlParser:
    """Wspólny parser dla wszystkich podstron listujących i statycznych na gov.pl."""

    def __init__(
        self,
        fetcher: GovResourceFetcher,
        *,
        include_pdf_attachments: bool = True,
        max_pdf_per_article: int = 2,
        min_text_length: int = 40,
    ) -> None:
        self.fetcher = fetcher
        self.include_pdf_attachments = include_pdf_attachments
        self.max_pdf_per_article = max_pdf_per_article
        self.min_text_length = min_text_length

    def fetch_page(self, url: str) -> GovPlDocument | None:
        """Pobiera i parsuje pojedynczą stronę artykułu lub stronę informacyjną gov.pl."""
        resource = self.fetcher.get(url)
        return self._parse_resource(resource)

    def fetch_listing(
        self,
        listing_url: str,
        *,
        limit: int = 8,
        fallback_to_page: bool = True,
    ) -> list[GovPlDocument]:
        """Pobiera listę najnowszych artykułów z sekcji gov.pl (np. /aktualnosci, /ostrzezenia)."""
        resource = self.fetcher.get(listing_url)
        links = extract_listing_links(
            resource.content,
            resource.url,
            encoding=resource.encoding,
            limit=limit,
        )

        docs: list[GovPlDocument] = []
        for article_url in links:
            try:
                doc = self.fetch_page(article_url)
                if doc is not None:
                    docs.append(doc)
            except Exception:
                # Awaria pojedynczego artykułu nie przerywa całej sekcji.
                continue

        # Jeśli strona nie jest listą (np. raport-dobowy3 z załącznikami PDF), parsuj ją samą.
        if not docs and fallback_to_page:
            single = self._parse_resource(resource)
            if single is not None:
                docs.append(single)

        return docs

    def _parse_resource(self, resource: RawResource) -> GovPlDocument | None:
        if "pdf" in resource.content_type or resource.content.startswith(b"%PDF"):
            pdf_text = extract_pdf_text(resource.content)
            if len(pdf_text) < self.min_text_length:
                return None
            return GovPlDocument(
                url=resource.url,
                title=resource.url.rsplit("/", 1)[-1],
                text=pdf_text,
                content_hash=content_hash(pdf_text),
                fetched_at=resource.fetched_at,
                department=_extract_department(resource.url),
            )

        title, body_text, published_at, pdf_links = parse_gov_article_html(
            resource.content,
            url=resource.url,
            encoding=resource.encoding,
        )

        attachments: list[GovAttachment] = []
        errors: list[str] = []
        combined_sections = [body_text] if body_text else []

        if self.include_pdf_attachments and pdf_links:
            for att_url, att_title in pdf_links[: self.max_pdf_per_article]:
                try:
                    att_res = self.fetcher.get(att_url)
                    if "pdf" not in att_res.content_type and not att_res.content.startswith(b"%PDF"):
                        continue
                    pdf_text = extract_pdf_text(att_res.content)
                    attachments.append(GovAttachment(url=att_res.url, title=att_title, text=pdf_text))
                    if pdf_text:
                        combined_sections.append(f"[Załącznik: {att_title}]\n{pdf_text}")
                except Exception as exc:
                    errors.append(f"Załącznik {att_url}: {exc}")

        full_text = normalize_text("\n\n".join(combined_sections))
        if len(full_text) < self.min_text_length:
            return None

        return GovPlDocument(
            url=resource.url,
            title=title,
            text=full_text,
            content_hash=content_hash(full_text),
            fetched_at=resource.fetched_at,
            published_at=published_at,
            department=_extract_department(resource.url),
            attachments=tuple(attachments),
            errors=tuple(errors),
        )