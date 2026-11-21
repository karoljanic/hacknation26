"""Offline gov.pl parser checks not covered by test_gov_pl_parser.py:
date parsing, attachment-failure isolation and the host allow-list."""

from datetime import datetime, timezone

import pytest

from prawdowid.adapters.sources.gov_pl import (
    GovPlHttpFetcher,
    GovPlParser,
    RawResource,
    parse_gov_date,
)

ARTICLE = b"""<html><body><main><article>
<h2>Komunikat MSWiA</h2><span class="event-date">20.11.2026</span>
<div class="editor-content">
<p>Od 1 grudnia obowiazuja nowe przepisy.</p>
<p>Druga linijka tresci komunikatu ministerstwa.</p>
<a href="/attachment/abc-123" class="file-download">Pobierz plik PDF</a>
</div></article></main></body></html>"""


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("21.11.2026 14:30", datetime(2026, 11, 21, 13, 30, tzinfo=timezone.utc)),
        ("2026-11-21T10:00:00Z", datetime(2026, 11, 21, 10, tzinfo=timezone.utc)),
        ("2026-07-01", datetime(2026, 6, 30, 22, tzinfo=timezone.utc)),  # naive = Warsaw (CEST)
        ("wczoraj", None),
        (None, None),
    ],
)
def test_parse_gov_date(raw, expected) -> None:
    assert parse_gov_date(raw) == expected


def test_parser_records_attachment_failure_without_failing_document() -> None:
    class Fetcher:
        def get(self, url):
            if "attachment" in url:
                raise RuntimeError("boom")
            return RawResource(url, ARTICLE, "text/html", datetime.now(timezone.utc), "utf-8")

    doc = GovPlParser(Fetcher()).fetch_page("https://www.gov.pl/web/mswia/komunikat")
    assert doc is not None and doc.department == "mswia"
    assert len(doc.errors) == 1 and "boom" in doc.errors[0]
    again = GovPlParser(Fetcher()).fetch_page("https://www.gov.pl/web/mswia/komunikat")
    assert again is not None and again.content_hash == doc.content_hash


def test_fetcher_rejects_non_gov_urls_before_any_request() -> None:
    with GovPlHttpFetcher() as fetcher, pytest.raises(ValueError):
        fetcher.get("https://example.com/")
