from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from prawdowid.adapters.sources.structured_data import PublicStructuredFetcher
from prawdowid.adapters.sources.web import FetchedResource


def test_public_structured_fetcher_parses_json_ld() -> None:
    transport = Mock()
    transport.fetch_resource.return_value = FetchedResource(
        url="https://api.example/data",
        body=b'{"value":7}',
        fetched_at=datetime.now(timezone.utc),
        content_type="application/ld+json",
        encoding="utf-8",
    )

    response = PublicStructuredFetcher(fetcher=transport).fetch(
        "https://api.example/data",
        allowed_hosts={"api.example"},
    )

    assert response.data == {"value": 7}
    transport.fetch_resource.assert_called_once()


def test_public_structured_fetcher_preserves_rss_xml() -> None:
    transport = Mock()
    transport.fetch_resource.return_value = FetchedResource(
        url="https://api.example/feed",
        body=b"<rss><channel><title>News</title></channel></rss>",
        fetched_at=datetime.now(timezone.utc),
        content_type="text/xml",
        encoding="utf-8",
    )

    response = PublicStructuredFetcher(fetcher=transport).fetch(
        "https://api.example/feed",
        allowed_hosts={"api.example"},
    )

    assert response.data == "<rss><channel><title>News</title></channel></rss>"


def test_public_structured_fetcher_surfaces_invalid_json() -> None:
    transport = Mock()
    transport.fetch_resource.return_value = FetchedResource(
        url="https://api.example/data",
        body=b"{invalid",
        fetched_at=datetime.now(timezone.utc),
        content_type="application/json",
    )

    with pytest.raises(ValueError, match="invalid JSON"):
        PublicStructuredFetcher(fetcher=transport).fetch(
            "https://api.example/data",
            allowed_hosts={"api.example"},
        )
