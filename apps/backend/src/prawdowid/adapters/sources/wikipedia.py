"""Polish Wikipedia via the public MediaWiki API (no key): full-text search and
plain-text article introductions, used as a citable source of established
general knowledge (history, geography, science)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import httpx

from prawdowid.adapters.sources.gov_api import json_client

MAX_INTRO_CHARS = 4000


@dataclass(frozen=True)
class WikipediaArticle:
    title: str
    url: str
    intro: str


class WikipediaClient:
    def __init__(self, *, lang: str = "pl", timeout: float = 10.0, client: httpx.Client | None = None) -> None:
        self.api_url = f"https://{lang}.wikipedia.org/w/api.php"
        self.lang = lang
        self._client = client or json_client(timeout)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> WikipediaClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _get(self, params: dict[str, str | int]) -> dict:
        response = self._client.get(self.api_url, params={**params, "format": "json", "formatversion": 2})
        response.raise_for_status()
        return response.json()

    def search(self, query: str, *, limit: int = 2) -> list[str]:
        """Titles of the best-matching articles."""
        if not query.strip():
            raise ValueError("Query must not be empty")
        data = self._get({"action": "query", "list": "search", "srsearch": query, "srlimit": limit})
        return [hit["title"] for hit in data.get("query", {}).get("search", []) if hit.get("title")]

    def intros(self, titles: Sequence[str]) -> list[WikipediaArticle]:
        """Plain-text introductions, in the order of ``titles``; missing pages are skipped."""
        if not titles:
            return []
        data = self._get(
            {
                "action": "query", "prop": "extracts|info", "inprop": "url", "explaintext": 1,
                "exintro": 1, "redirects": 1, "titles": "|".join(titles),
            }
        )
        pages = {page.get("title"): page for page in data.get("query", {}).get("pages", [])}
        redirects = {item["from"]: item["to"] for item in data.get("query", {}).get("redirects", [])}
        articles: list[WikipediaArticle] = []
        for title in titles:
            page = pages.get(redirects.get(title, title))
            intro = ((page or {}).get("extract") or "").strip()
            if page is None or page.get("missing") or not intro:
                continue
            articles.append(
                WikipediaArticle(title=page["title"], url=page["fullurl"], intro=intro[:MAX_INTRO_CHARS])
            )
        return articles
