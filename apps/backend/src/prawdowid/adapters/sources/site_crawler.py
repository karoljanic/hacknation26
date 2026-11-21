import http.client
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urldefrag, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup
from bs4.element import Tag

from prawdowid.adapters.sources.web import FetchedArticle, PublicWebFetcher

CRAWLER_USER_AGENT = "PrawdowidCrawler"
FETCH_ERRORS = (http.client.HTTPException, OSError, RuntimeError, TimeoutError, ValueError)
DOCUMENT_SUFFIXES = {
    ".7z",
    ".csv",
    ".doc",
    ".docx",
    ".gif",
    ".jpeg",
    ".jpg",
    ".json",
    ".mp3",
    ".mp4",
    ".pdf",
    ".png",
    ".ppt",
    ".pptx",
    ".rar",
    ".svg",
    ".xls",
    ".xlsx",
    ".zip",
}


@dataclass(frozen=True)
class PageDescription:
    final_url: str
    retrieved_at: str
    title: str | None
    meta_description: str | None
    headings: list[str]
    excerpt: str
    description: str


@dataclass(frozen=True)
class DirectLink:
    url: str
    labels: list[str]
    contexts: list[str]


@dataclass(frozen=True)
class SubpageResult:
    link: DirectLink
    status: str
    page: PageDescription | None = None
    error: str | None = None


@dataclass(frozen=True)
class SourceCrawlResult:
    source_url: str
    status: str
    root: PageDescription | None
    subpages: list[SubpageResult]
    documents: list[DirectLink]
    error: str | None = None


@dataclass(frozen=True)
class ParsedPage:
    description: PageDescription
    subpages: list[DirectLink]
    documents: list[DirectLink]


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _normalized_host(url: str) -> str | None:
    hostname = urlsplit(url).hostname
    if hostname is None:
        return None
    return hostname.encode("idna").decode("ascii").lower()


def _origin(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), "", "", ""))


def _is_document(url: str) -> bool:
    suffix = urlsplit(url).path.lower().rsplit("/", 1)[-1]
    return any(suffix.endswith(extension) for extension in DOCUMENT_SUFFIXES)


def _link_context(anchor: Tag) -> str:
    if anchor.find_parent(["nav", "header", "footer", "aside"]) is not None:
        return "navigation"
    return "content"


def describe_html(article: FetchedArticle) -> ParsedPage:
    soup = BeautifulSoup(article.body, "html.parser", from_encoding=article.encoding)
    title_node = soup.find("title")
    title = _clean_text(title_node.get_text(" ", strip=True)) if title_node else None
    meta = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    if meta is None:
        meta = soup.find("meta", attrs={"property": re.compile("^og:description$", re.I)})
    meta_description = _clean_text(str(meta.get("content", ""))) if meta else None
    if not meta_description:
        meta_description = None

    extracted: dict[str, dict[str, list[str]]] = {"subpages": {}, "documents": {}}
    page_host = _normalized_host(article.url)
    if page_host is not None:
        for anchor in soup.find_all("a", href=True):
            href = str(anchor.get("href", "")).strip()
            candidate = urljoin(article.url, href)
            candidate, fragment = urldefrag(candidate)
            parsed = urlsplit(candidate)
            if (
                parsed.scheme.lower() not in {"http", "https"}
                or parsed.username is not None
                or parsed.password is not None
                or _normalized_host(candidate) != page_host
            ):
                continue
            if candidate == urldefrag(article.url)[0]:
                if not fragment.startswith("/"):
                    continue
                candidate = f"{candidate}#{fragment}"
            elif fragment.startswith("/"):
                candidate = f"{candidate}#{fragment}"

            section = "documents" if _is_document(candidate) else "subpages"
            entry = extracted[section].setdefault(candidate, {"labels": [], "contexts": []})
            label = _clean_text(anchor.get_text(" ", strip=True)) or _clean_text(
                str(anchor.get("title", ""))
            )
            context = _link_context(anchor)
            if label and label not in entry["labels"]:
                entry["labels"].append(label)
            if context not in entry["contexts"]:
                entry["contexts"].append(context)

    content = soup.find("main") or soup.find("article") or soup.body or soup
    for node in content.select("script, style, noscript, template, svg, form, nav, header, footer, aside"):
        node.decompose()
    headings = []
    for heading in content.find_all(["h1", "h2", "h3"]):
        text = _clean_text(heading.get_text(" ", strip=True))
        if text and text not in headings:
            headings.append(text)
    paragraphs = []
    for block in content.find_all(["p", "li", "blockquote"]):
        if block.find(["p", "li", "blockquote"]) is not None:
            continue
        text = _clean_text(block.get_text(" ", strip=True))
        if text and text not in paragraphs:
            paragraphs.append(text)
    excerpt = " ".join(paragraphs)[:1600]
    if meta_description:
        description = meta_description
    elif excerpt:
        description = excerpt[:700]
    elif headings:
        description = "Widoczne sekcje: " + "; ".join(headings[:8])
    else:
        description = (
            "W pobranym HTML nie znaleziono widocznej treści; strona może wymagać JavaScript."
        )

    page_description = PageDescription(
        final_url=article.url,
        retrieved_at=article.fetched_at.astimezone(timezone.utc).isoformat(),
        title=title,
        meta_description=meta_description,
        headings=headings[:20],
        excerpt=excerpt,
        description=description,
    )
    return ParsedPage(
        description=page_description,
        subpages=[
            DirectLink(url=url, labels=item["labels"], contexts=item["contexts"])
            for url, item in extracted["subpages"].items()
        ],
        documents=[
            DirectLink(url=url, labels=item["labels"], contexts=item["contexts"])
            for url, item in extracted["documents"].items()
        ],
    )


class DirectSourceCrawler:
    def __init__(
        self,
        *,
        fetcher: PublicWebFetcher | None = None,
        delay_seconds: float = 1.0,
    ) -> None:
        if delay_seconds < 0:
            raise ValueError("Request delay must not be negative")
        self.fetcher = fetcher or PublicWebFetcher()
        self.delay_seconds = delay_seconds
        self._robots: dict[str, RobotFileParser] = {}
        self._crawl_delays: dict[str, float] = {}
        self._last_request: dict[str, float] = {}
        self._page_cache: dict[str, ParsedPage] = {}

    def crawl(self, source_url: str) -> SourceCrawlResult:
        try:
            if not self._can_fetch(source_url):
                return SourceCrawlResult(source_url, "robots_disallowed", None, [], [])
            root = self._get_page(source_url)
        except FETCH_ERRORS as error:
            return SourceCrawlResult(
                source_url, "fetch_error", None, [], [], f"{type(error).__name__}: {error}"
            )

        final_url = root.description.final_url
        try:
            if _origin(final_url) != _origin(source_url) and not self._can_fetch(final_url):
                return SourceCrawlResult(
                    source_url,
                    "redirected_to_robots_disallowed_page",
                    root.description,
                    [],
                    root.documents,
                )
        except FETCH_ERRORS as error:
            return SourceCrawlResult(
                source_url,
                "robots_error",
                root.description,
                [],
                root.documents,
                f"{type(error).__name__}: {error}",
            )

        subpages = []
        allowed_host = _normalized_host(final_url)
        for link in root.subpages:
            try:
                if not self._can_fetch(link.url):
                    subpages.append(SubpageResult(link, "robots_disallowed"))
                    continue
                page = self._get_page(
                    link.url,
                    allowed_hosts={allowed_host} if allowed_host is not None else set(),
                )
                subpages.append(SubpageResult(link, "fetched", page.description))
            except FETCH_ERRORS as error:
                subpages.append(
                    SubpageResult(
                        link,
                        "fetch_error",
                        error=f"{type(error).__name__}: {error}",
                    )
                )

        return SourceCrawlResult(
            source_url,
            "fetched",
            root.description,
            subpages,
            root.documents,
        )

    def _can_fetch(self, url: str) -> bool:
        parser = self._robots_for(url)
        return parser.can_fetch(CRAWLER_USER_AGENT, url)

    def _robots_for(self, url: str) -> RobotFileParser:
        origin = _origin(url)
        cached = self._robots.get(origin)
        if cached is not None:
            return cached
        parsed = urlsplit(url)
        host = _normalized_host(url)
        if host is None:
            raise ValueError("URL has no host")
        robots_url = urlunsplit((parsed.scheme, parsed.netloc, "/robots.txt", "", ""))
        response = self._request(robots_url, allowed_hosts={host})
        parser = RobotFileParser(robots_url)
        if response.status_code in {404, 410}:
            parser.parse(["User-agent: *", "Allow: /"])
        elif response.status_code == 200:
            text = response.body.decode(response.encoding or "utf-8", errors="replace")
            parser.parse(text.splitlines())
        else:
            raise RuntimeError(f"Unexpected robots.txt status {response.status_code}")
        crawl_delay = parser.crawl_delay(CRAWLER_USER_AGENT)
        self._crawl_delays[origin] = float(crawl_delay or 0)
        self._robots[origin] = parser
        return parser

    def _get_page(
        self, url: str, *, allowed_hosts: set[str] | None = None
    ) -> ParsedPage:
        request_url = urldefrag(url)[0]
        cached = self._page_cache.get(request_url)
        if cached is not None:
            return cached
        article = self._request(url, allowed_hosts=allowed_hosts)
        page = describe_html(article)
        self._page_cache[request_url] = page
        return page

    def _request(
        self, url: str, *, allowed_hosts: set[str] | None = None
    ) -> FetchedArticle:
        origin = _origin(url)
        delay = max(self.delay_seconds, self._crawl_delays.get(origin, 0))
        previous = self._last_request.get(origin)
        if previous is not None:
            wait = delay - (time.monotonic() - previous)
            if wait > 0:
                time.sleep(wait)
        try:
            return self.fetcher.fetch(url, allowed_hosts=allowed_hosts)
        finally:
            self._last_request[origin] = time.monotonic()


