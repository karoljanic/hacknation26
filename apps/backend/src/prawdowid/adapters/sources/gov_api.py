"""Public JSON APIs of government sites, queried live per claim.

* gov.pl site search (``/api/data/search``): the same endpoint as the search
  box on www.gov.pl, across every ministry, office and embassy section.
* MSZ Odyseusz country profiles (``/api/v1/informacje/profile-panstw/{ISO}``):
  current travel advisories, warning levels and notices (strikes, protests,
  disasters) for foreign countries.

Neither needs a key. Both return structured data, so no HTML parsing is needed.
"""

from __future__ import annotations

import os
import ssl
from dataclasses import dataclass
from datetime import date, datetime
from urllib.parse import urljoin, urlsplit

import httpx

GOV_PL_BASE = "https://www.gov.pl"
GOV_SEARCH_URL = f"{GOV_PL_BASE}/api/data/search"
ODYSEUSZ_BASE = "https://odyseusz.gov.pl"
ODYSEUSZ_PROFILE_URL = f"{ODYSEUSZ_BASE}/api/v1/informacje/profile-panstw/"
USER_AGENT = "Prawdowid/0.1 (+weryfikacja informacji)"


def json_client(timeout: float) -> httpx.Client:
    verify: bool | ssl.SSLContext = True
    ca_bundle = os.environ.get("SSL_CERT_FILE")
    if ca_bundle:
        verify = ssl.create_default_context(cafile=ca_bundle)
    return httpx.Client(
        timeout=httpx.Timeout(timeout),
        verify=verify,
        follow_redirects=False,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json", "Accept-Language": "pl-PL,pl;q=0.9"},
    )


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass(frozen=True)
class GovSearchHit:
    title: str
    intro: str
    url: str
    section: str | None
    published_at: datetime | None


class GovPlSearchClient:
    def __init__(self, *, timeout: float = 10.0, client: httpx.Client | None = None) -> None:
        self._client = client or json_client(timeout)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> GovPlSearchClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def search(self, query: str, *, size: int = 10) -> list[GovSearchHit]:
        if not query.strip():
            raise ValueError("Query must not be empty")
        response = self._client.get(GOV_SEARCH_URL, params={"query": query, "size": size})
        response.raise_for_status()
        hits: list[GovSearchHit] = []
        for raw in response.json().get("results") or []:
            title = (raw.get("title") or "").strip()
            url = urljoin(GOV_PL_BASE, raw.get("url") or "")
            # Only gov.pl pages: a result must never point the fetcher elsewhere.
            if not title or urlsplit(url).hostname not in {"www.gov.pl", "gov.pl"}:
                continue
            hits.append(
                GovSearchHit(
                    title=title,
                    intro=(raw.get("intro") or "").strip(),
                    url=url,
                    section=raw.get("path") or None,
                    published_at=_parse_datetime(raw.get("publishDate")),
                )
            )
        return hits


@dataclass(frozen=True)
class CountryAdvisory:
    code: str
    name: str
    page_url: str
    updated_at: date | None
    notice: str | None
    warnings: list[str]
    sections: list[tuple[str, str]]  # (topic, summary)


class OdyseuszClient:
    def __init__(self, *, timeout: float = 10.0, client: httpx.Client | None = None) -> None:
        self._client = client or json_client(timeout)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> OdyseuszClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def country(self, code: str) -> CountryAdvisory | None:
        """``None`` when MSZ has no profile for the code (the API answers 204)."""
        code = code.strip().upper()
        if len(code) != 2 or not code.isascii() or not code.isalpha():
            raise ValueError(f"Not an ISO 3166-1 alpha-2 code: {code!r}")
        response = self._client.get(ODYSEUSZ_PROFILE_URL + code)
        if response.status_code in (204, 404):
            return None
        response.raise_for_status()
        data = response.json()
        warnings = []
        for warning in data.get("ostrzezenia") or []:
            level = ((warning.get("poziomZagrozenia") or {}).get("opis") or "").strip()
            scope = ((warning.get("zakresTerytorialnyOstrzezenia") or {}).get("opis") or "").strip()
            text = (warning.get("tresc") or "").strip()
            parts = [part for part in (level, scope, text) if part]
            if parts:
                warnings.append(" – ".join(parts))
        sections = []
        for section in data.get("sekcje") or []:
            topic = ((section.get("zakresInformacyjny") or {}).get("opis") or "").strip()
            summary = (section.get("podsumowanie") or "").strip()
            if topic and summary:
                sections.append((topic, summary))
        updated = _parse_datetime(data.get("dataAktualizacji"))
        return CountryAdvisory(
            code=code,
            name=(data.get("nazwa") or code).strip(),
            page_url=f"{ODYSEUSZ_BASE}/{code}",
            updated_at=updated.date() if updated else None,
            notice=(data.get("trescPowiadomieniaDluga") or "").strip() or None,
            warnings=warnings,
            sections=sections,
        )
