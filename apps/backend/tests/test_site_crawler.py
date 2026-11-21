from datetime import datetime, timezone

from prawdowid.adapters.sources.site_crawler import DirectSourceCrawler, describe_html
from prawdowid.adapters.sources.web import FetchedArticle


class FixtureFetcher:
    def __init__(self, pages: dict[str, tuple[bytes, str]]) -> None:
        self.pages = pages
        self.requests: list[str] = []

    def fetch(self, url: str, *, allowed_hosts: set[str] | None = None) -> FetchedArticle:
        self.requests.append(url)
        body, content_type = self.pages[url]
        if url.endswith("/robots.txt"):
            return FetchedArticle(
                url=url,
                body=body,
                fetched_at=datetime.now(timezone.utc),
                encoding="utf-8",
                status_code=200,
            )
        assert content_type == "text/html"
        return FetchedArticle(
            url=url,
            body=body,
            fetched_at=datetime.now(timezone.utc),
            encoding="utf-8",
        )


def test_describe_html_lists_same_host_links_and_marks_javascript_shell() -> None:
    html = b"""
    <html><head><title>Source</title><meta name="description" content="A source page"></head>
    <body><main><h1>Source heading</h1><p>Visible factual content.</p>
    <a href="/child">Child page</a><a href="/download.pdf">Report</a>
    <a href="https://external.example/page">External</a></main></body></html>
    """
    page = describe_html(
        FetchedArticle(
            url="https://example.test/source",
            body=html,
            fetched_at=datetime.now(timezone.utc),
            encoding="utf-8",
        )
    )
    shell = describe_html(
        FetchedArticle(
            url="https://example.test/app",
            body=b"<html><body><app-root></app-root><script>render()</script></body></html>",
            fetched_at=datetime.now(timezone.utc),
        )
    )

    assert page.description.description == "A source page"
    assert page.description.headings == ["Source heading"]
    assert [link.url for link in page.subpages] == ["https://example.test/child"]
    assert [link.url for link in page.documents] == ["https://example.test/download.pdf"]
    assert "JavaScript" in shell.description.description


def test_crawler_fetches_one_level_only_and_respects_robots() -> None:
    root = "http://example.test/"
    child = "http://example.test/child"
    fetcher = FixtureFetcher(
        {
            "http://example.test/robots.txt": (b"User-agent: *\nAllow: /", "text/plain"),
            root: (
                b"<html><head><title>Root</title></head><main><h1>Root</h1><p>Root facts.</p>"
                b'<a href="/child">Child</a></main></html>',
                "text/html",
            ),
            child: (
                b"<html><head><title>Child</title></head><main><h1>Child</h1><p>Child facts.</p>"
                b'<a href="/grandchild">Grandchild</a></main></html>',
                "text/html",
            ),
        }
    )

    result = DirectSourceCrawler(fetcher=fetcher, delay_seconds=0).crawl(root)

    assert result.status == "fetched"
    assert result.root is not None and result.root.title == "Root"
    assert len(result.subpages) == 1
    assert result.subpages[0].page is not None
    assert result.subpages[0].page.title == "Child"
    assert "http://example.test/grandchild" not in fetcher.requests


def test_crawler_skips_disallowed_seed_without_fetching_page() -> None:
    root = "http://example.test/private"
    fetcher = FixtureFetcher(
        {
            "http://example.test/robots.txt": (
                b"User-agent: *\nDisallow: /private",
                "text/plain",
            )
        }
    )

    result = DirectSourceCrawler(fetcher=fetcher, delay_seconds=0).crawl(root)

    assert result.status == "robots_disallowed"
    assert fetcher.requests == ["http://example.test/robots.txt"]
