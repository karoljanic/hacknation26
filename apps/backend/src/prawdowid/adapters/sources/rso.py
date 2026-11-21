"""Parser i konektor dla Regionalnego Systemu Ostrzegania (RSO – XML TVP).

Zastępuje scrapowanie JS-owej strony gov.pl/web/gov/ostrzezenia oficjalnym
kanałem XML udostępnianym przez TVP S.A. na zlecenie MSWiA:
https://komunikaty.tvp.pl/komunikatyxml/wszystkie/wszystkie/0?_format=xml
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import os
import ssl
import time
from typing import Protocol
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

import httpx

from prawdowid.adapters.sources.gov_pl import (
    RawResource,
    content_hash,
    normalize_text,
    parse_gov_date,
    sanitize_soup,
    sort_latest,
)

RSO_ALL_XML_URL = "https://komunikaty.tvp.pl/komunikatyxml/wszystkie/wszystkie/0?_format=xml"
RSO_PUBLIC_PAGE_URL = "https://www.gov.pl/web/gov/ostrzezenia"
ALLOWED_HOSTS = frozenset({"komunikaty.tvp.pl"})
DEFAULT_USER_AGENT = "Prawdowid/0.1 (rso xml ingestion)"


class RsoResourceFetcher(Protocol):
    def get(self, url: str) -> RawResource: ...


@dataclass(frozen=True)
class RsoDocument:
    """Znormalizowany komunikat RSO gotowy do zapisu w tabeli ``document``."""

    url: str
    title: str
    text: str
    content_hash: str
    fetched_at: datetime
    published_at: datetime | None = None
    expires_at: datetime | None = None
    category: str | None = None
    provinces: tuple[str, ...] = ()
    lang: str = "pl"
    external_id: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)


class RsoHttpFetcher:
    """Lekki klient HTTP dla oficjalnego XML RSO (komunikaty.tvp.pl)."""

    def __init__(
        self,
        *,
        timeout: float = 15.0,
        max_bytes: int = 5_000_000,
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
            max_redirects=3,
            verify=verify,
            headers={
                "User-Agent": user_agent,
                "Accept": "application/xml,text/xml;q=0.9,*/*;q=0.5",
                "Accept-Language": "pl-PL,pl;q=0.9",
            },
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> RsoHttpFetcher:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def get(self, url: str) -> RawResource:
        _ensure_rso_url(url)
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                with self._client.stream("GET", url) as response:
                    final_url = str(response.url)
                    _ensure_rso_url(final_url)
                    if response.status_code in {429, 500, 502, 503, 504} and attempt < self.max_retries:
                        time.sleep(0.5 * (2**attempt))
                        continue
                    if response.status_code != 200:
                        raise RuntimeError(f"RSO HTTP {response.status_code} dla {final_url}")

                    buf = bytearray()
                    for chunk in response.iter_bytes():
                        buf.extend(chunk)
                        if len(buf) > self.max_bytes:
                            raise ValueError(
                                f"Odpowiedź RSO przekracza limit {self.max_bytes} B: {final_url}"
                            )

                    ctype = response.headers.get("content-type", "").split(";")[0].strip().lower()
                    return RawResource(
                        url=final_url,
                        content=bytes(buf),
                        content_type=ctype,
                        fetched_at=datetime.now(timezone.utc),
                        encoding=response.encoding or "utf-8",
                    )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_exc = exc
                if attempt >= self.max_retries:
                    raise RuntimeError(f"Błąd sieci RSO dla {url}: {exc}") from exc
                time.sleep(0.5 * (2**attempt))

        raise RuntimeError(f"Nie udało się pobrać {url}: {last_exc}")


# --------------------------------------------------------------------------- #
# Czyste funkcje parsujące XML RSO (testowalne bez sieci)
# --------------------------------------------------------------------------- #


def _ensure_rso_url(url: str) -> None:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or host not in ALLOWED_HOSTS:
        raise ValueError(f"Obsługiwane są wyłącznie adresy komunikaty.tvp.pl: {url}")


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


def _clean_maybe_html(raw: str) -> str:
    if not raw:
        return ""
    if "<" in raw and ">" in raw:
        return normalize_text(sanitize_soup(raw).get_text(" ", strip=True))
    return normalize_text(raw)


def _extract_provinces(elem: ET.Element) -> tuple[str, ...]:
    provinces: list[str] = []
    seen: set[str] = set()
    for node in elem.iter():
        if _local_tag(node.tag) in {"province", "wojewodztwo"}:
            name = _child_text(node, ("name", "nazwa")) or normalize_text("".join(node.itertext()))
            if name and name.lower() not in seen:
                seen.add(name.lower())
                provinces.append(name)
    return tuple(provinces)


def parse_rso_xml(
    xml_bytes: bytes | str,
    *,
    fetched_at: datetime,
    source_url: str = RSO_ALL_XML_URL,
    province: str | None = None,
    limit: int | None = None,
) -> list[RsoDocument]:
    """Parsuje XML RSO do listy znormalizowanych dokumentów posortowanych od najnowszych."""
    raw_bytes = xml_bytes.encode("utf-8") if isinstance(xml_bytes, str) else xml_bytes
    root = ET.fromstring(raw_bytes)

    nodes = [el for el in root.iter() if _local_tag(el.tag) in {"news", "komunikat", "item"}]
    wanted_province = province.strip().lower() if province else None
    docs: list[RsoDocument] = []

    for idx, node in enumerate(nodes, start=1):
        title = _clean_maybe_html(_child_text(node, ("title", "tytul")))
        shortcut = _clean_maybe_html(_child_text(node, ("shortcut", "skrot", "lead")))
        body = _clean_maybe_html(_child_text(node, ("content", "tresc", "description")))
        if not title and not body:
            continue

        provinces = _extract_provinces(node)
        if wanted_province and not any(wanted_province in p.lower() for p in provinces):
            continue

        ext_id = _child_text(node, ("id", "guid")) or str(idx)
        category = (
            _clean_maybe_html(_child_text(node, ("category", "kategoria", "type", "typ"))) or None
        )
        valid_from = _child_text(node, ("valid_from", "data_od", "obowiazuje_od"))
        valid_to = _child_text(node, ("valid_to", "data_do", "obowiazuje_do"))
        published_raw = _child_text(
            node, ("created", "modified", "pubdate", "data_publikacji", "valid_from")
        )
        item_url = _child_text(node, ("url", "link")) or f"{RSO_PUBLIC_PAGE_URL}#rso-{ext_id}"

        lines = [f"Komunikat Regionalnego Systemu Ostrzegania (RSO): {title or 'Ostrzeżenie'}"]
        if category:
            lines.append(f"Kategoria: {category}.")
        if provinces:
            lines.append(f"Województwa: {', '.join(provinces)}.")
        if valid_from or valid_to:
            lines.append(f"Okres obowiązywania: od {valid_from or '?'} do {valid_to or '?'}.")
        if shortcut and shortcut != body:
            lines.append(f"Skrót: {shortcut}")
        if body:
            lines.append(body)

        text = normalize_text("\n".join(lines))
        if len(text) < 20:
            continue

        docs.append(
            RsoDocument(
                url=item_url,
                title=title or f"Komunikat RSO #{ext_id}",
                text=text,
                content_hash=content_hash(text),
                fetched_at=fetched_at,
                published_at=parse_gov_date(published_raw),
                expires_at=parse_gov_date(valid_to),
                category=category,
                provinces=provinces,
                external_id=ext_id,
                metadata={"feed_url": source_url},
            )
        )

    return sort_latest(docs, limit=limit)


class RsoParser:
    """Konektor do oficjalnego feedu XML Regionalnego Systemu Ostrzegania."""

    def __init__(self, fetcher: RsoResourceFetcher) -> None:
        self.fetcher = fetcher

    def fetch_latest(
        self,
        *,
        limit: int = 10,
        province: str | None = None,
        url: str = RSO_ALL_XML_URL,
    ) -> list[RsoDocument]:
        """Pobiera do ``limit`` najnowszych komunikatów RSO."""
        return self.fetch_warnings(url=url, province=province, limit=limit)

    def fetch_warnings(
        self,
        *,
        url: str = RSO_ALL_XML_URL,
        province: str | None = None,
        limit: int | None = 30,
    ) -> list[RsoDocument]:
        res = self.fetcher.get(url)
        return parse_rso_xml(
            res.content,
            fetched_at=res.fetched_at,
            source_url=res.url,
            province=province,
            limit=limit,
        )