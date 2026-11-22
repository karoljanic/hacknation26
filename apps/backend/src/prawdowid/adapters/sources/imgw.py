"""Parser i konektor dla otwartego REST API IMGW-PIB (danepubliczne.imgw.pl).

Pokrywa obszary z portali meteo.imgw.pl, hydro.imgw.pl i baltyk.imgw.pl:
- ostrzeżenia meteorologiczne (warningsmeteo),
- ostrzeżenia hydrologiczne (warningshydro),
- bieżące dane synoptyczne (synop),
- bieżące stany wód (hydro).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import logging
import os
import ssl
import time
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

import httpx

from prawdowid.adapters.sources.gov_pl import (
    RawResource,
    content_hash,
    normalize_text,
    parse_gov_date,
    sort_latest,
)

log = logging.getLogger(__name__)

ImgwDataset = Literal["warningsmeteo", "warningshydro", "synop", "hydro"]

IMGW_API_BASE = "https://danepubliczne.imgw.pl/api/data"
ALLOWED_HOSTS = frozenset({"danepubliczne.imgw.pl"})
DEFAULT_USER_AGENT = "Prawdowid/0.1 (imgw corpus ingestion)"
_MIN_UTC = datetime.min.replace(tzinfo=timezone.utc)

ENDPOINT_URLS: dict[ImgwDataset, str] = {
    "warningsmeteo": f"{IMGW_API_BASE}/warningsmeteo",
    "warningshydro": f"{IMGW_API_BASE}/warningshydro",
    "synop": f"{IMGW_API_BASE}/synop",
    "hydro": f"{IMGW_API_BASE}/hydro",
}

MAJOR_SYNOP_STATIONS = frozenset(
    {
        "warszawa",
        "kraków",
        "gdańsk",
        "wrocław",
        "poznań",
        "łódź",
        "katowice",
        "szczecin",
        "białystok",
        "lublin",
        "bydgoszcz",
        "rzeszów",
        "toruń",
        "olsztyn",
        "opole",
        "kielce",
        "zielona góra",
        "gorzów",
        "koszalin",
        "hel",
        "ustka",
        "świnoujście",
    }
)


class ImgwResourceFetcher(Protocol):
    def get(self, url: str) -> RawResource: ...


@dataclass(frozen=True)
class ImgwDocument:
    """Znormalizowany dokument IMGW gotowy do zapisu w tabeli ``document``."""

    url: str
    title: str
    text: str
    content_hash: str
    fetched_at: datetime
    dataset: ImgwDataset
    published_at: datetime | None = None
    expires_at: datetime | None = None
    lang: str = "pl"
    metadata: dict[str, Any] = field(default_factory=dict)


class _RetryableStatus(RuntimeError):
    pass


class ImgwHttpFetcher:
    """Lekki klient HTTP dla otwartego API danepubliczne.imgw.pl (weryfikuje host na każdym przekierowaniu)."""

    def __init__(
        self,
        *,
        timeout: float = 15.0,
        max_bytes: int = 8_000_000,
        max_retries: int = 2,
        max_redirects: int = 3,
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
                "Accept": "application/json",
                "Accept-Language": "pl-PL,pl;q=0.9",
            },
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> ImgwHttpFetcher:
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
                    raise RuntimeError(f"Błąd sieci IMGW dla {url}: {exc}") from exc
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
            _ensure_imgw_url(current)
            with self._client.stream("GET", current) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location or hop == self.max_redirects:
                        raise RuntimeError(f"Przekroczono limit przekierowań IMGW dla {current}")
                    current = str(response.url.join(location))
                    continue
                if response.status_code in {429, 500, 502, 503, 504}:
                    raise _RetryableStatus(f"IMGW HTTP {response.status_code} dla {current}")
                if response.status_code != 200:
                    raise RuntimeError(f"IMGW HTTP {response.status_code} dla {current}")

                buf = bytearray()
                for chunk in response.iter_bytes():
                    buf.extend(chunk)
                    if len(buf) > self.max_bytes:
                        raise ValueError(
                            f"Odpowiedź IMGW przekracza limit {self.max_bytes} B: {current}"
                        )

                ctype = response.headers.get("content-type", "").split(";")[0].strip().lower()
                return RawResource(
                    url=str(response.url),
                    content=bytes(buf),
                    content_type=ctype,
                    fetched_at=datetime.now(timezone.utc),
                    encoding=response.encoding or "utf-8",
                )
        raise RuntimeError(f"Przekroczono limit przekierowań IMGW dla {url}")


# --------------------------------------------------------------------------- #
# Czyste funkcje parsujące (testowalne bez zapytań sieciowych)
# --------------------------------------------------------------------------- #


def _ensure_imgw_url(url: str) -> None:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or host not in ALLOWED_HOSTS:
        raise ValueError(f"Obsługiwane są wyłącznie adresy danepubliczne.imgw.pl: {url}")


def _decode_json_list(payload: bytes | str) -> list[dict[str, Any]]:
    raw = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else payload
    data = json.loads(raw)
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict) and data.get("status") is False:
        return []
    return []


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", "."))
    except ValueError:
        return None


def _pick_first(item: dict[str, Any], *keys: str) -> str:
    for key in keys:
        val = item.get(key)
        if val is not None and str(val).strip():
            return normalize_text(str(val))
    return ""


def _parse_expires_at(raw: str) -> datetime | None:
    """Parsuje datę wygaśnięcia; rok >= 9000 (znacznik 'do odwołania' w IMGW) mapuje na None."""
    dt = parse_gov_date(raw)
    if dt is not None and dt.year >= 9000:
        return None
    return dt


def parse_warnings_meteo(
    payload: bytes | str,
    *,
    fetched_at: datetime,
    source_url: str = ENDPOINT_URLS["warningsmeteo"],
    limit: int | None = None,
) -> list[ImgwDocument]:
    items = _decode_json_list(payload)
    docs: list[ImgwDocument] = []

    for idx, item in enumerate(items, start=1):
        event = (
            _pick_first(item, "nazwa_zdarzenia", "zdarzenie", "zjawisko")
            or "Ostrzeżenie meteorologiczne"
        )
        level = _pick_first(item, "stopien")
        prob = _pick_first(item, "prawdopodobienstwo")
        valid_from = _pick_first(item, "obowiazuje_od")
        valid_to = _pick_first(item, "obowiazuje_do")
        published_raw = _pick_first(item, "opublikowano", "obowiazuje_od")
        body = _pick_first(item, "tresc")
        comment = _pick_first(item, "komentarz")
        office = _pick_first(item, "biuro")
        warning_id = _pick_first(item, "id") or str(idx)
        expires_at = _parse_expires_at(valid_to)

        teryts = item.get("teryt")
        teryt_list = [str(t).strip() for t in teryts if str(t).strip()] if isinstance(teryts, list) else []

        title = f"Ostrzeżenie meteorologiczne IMGW{f' ({level} st.)' if level else ''}: {event}"
        lines = [title]
        if valid_from or valid_to:
            valid_to_label = "do odwołania" if (valid_to and expires_at is None) else (valid_to or "?")
            lines.append(f"Okres obowiązywania: od {valid_from or '?'} do {valid_to_label}.")
        if prob:
            lines.append(f"Prawdopodobieństwo wystąpienia: {prob}%.")
        if office:
            lines.append(f"Biuro wydające: {office}.")
        if body:
            lines.append(f"Treść ostrzeżenia: {body}")
        if comment:
            lines.append(f"Komentarz: {comment}")
        if teryt_list:
            lines.append(
                f"Kody TERYT objętych powiatów ({len(teryt_list)}): {', '.join(teryt_list[:25])}."
            )

        text = normalize_text("\n".join(lines))
        docs.append(
            ImgwDocument(
                url=f"{source_url}#meteo-{warning_id}",
                title=title,
                text=text,
                content_hash=content_hash(text),
                fetched_at=fetched_at,
                dataset="warningsmeteo",
                published_at=parse_gov_date(published_raw),
                expires_at=expires_at,
                metadata={
                    "warning_id": warning_id,
                    "event": event,
                    "level": level,
                    "teryt": teryt_list,
                },
            )
        )

    return sort_latest(docs, limit=limit)


def parse_warnings_hydro(
    payload: bytes | str,
    *,
    fetched_at: datetime,
    source_url: str = ENDPOINT_URLS["warningshydro"],
    limit: int | None = None,
) -> list[ImgwDocument]:
    items = _decode_json_list(payload)
    docs: list[ImgwDocument] = []

    for idx, item in enumerate(items, start=1):
        event = (
            _pick_first(item, "zdarzenie", "zjawisko", "nazwa_zdarzenia")
            or "Ostrzeżenie hydrologiczne"
        )
        level = _pick_first(item, "stopien")
        number = _pick_first(item, "numer") or str(idx)
        prob = _pick_first(item, "prawdopodobienstwo")
        valid_from = _pick_first(item, "data_od_utc", "data_od", "obowiazuje_od")
        valid_to = _pick_first(item, "data_do_utc", "data_do", "obowiazuje_do")
        published_raw = _pick_first(item, "opublikowano", "data_od_utc", "data_od")
        course = _pick_first(item, "przebieg", "tresc")
        comment = _pick_first(item, "komentarz")
        office = _pick_first(item, "biuro")
        expires_at = _parse_expires_at(valid_to)

        areas_raw = item.get("obszary")
        area_descriptions: list[str] = []
        provinces: set[str] = set()
        if isinstance(areas_raw, list):
            for area in areas_raw:
                if not isinstance(area, dict):
                    continue
                prov = _pick_first(area, "wojewodztwo")
                desc = _pick_first(area, "opis")
                if prov:
                    provinces.add(prov)
                if desc:
                    area_descriptions.append(f"{desc} ({prov})" if prov else desc)

        title = f"Ostrzeżenie hydrologiczne IMGW nr {number}{f' ({level} st.)' if level else ''}: {event}"
        lines = [title]
        if valid_from or valid_to:
            valid_to_label = "do odwołania" if (valid_to and expires_at is None) else (valid_to or "?")
            lines.append(f"Okres obowiązywania: od {valid_from or '?'} do {valid_to_label}.")
        if provinces:
            lines.append(f"Województwa: {', '.join(sorted(provinces))}.")
        if area_descriptions:
            lines.append(f"Obszary/zlewnie: {'; '.join(area_descriptions[:15])}.")
        if prob:
            lines.append(f"Prawdopodobieństwo: {prob}%.")
        if office:
            lines.append(f"Biuro wydające: {office}.")
        if course:
            lines.append(f"Przebieg: {course}")
        if comment:
            lines.append(f"Komentarz: {comment}")

        text = normalize_text("\n".join(lines))
        docs.append(
            ImgwDocument(
                url=f"{source_url}#hydro-{number}-{idx}",
                title=title,
                text=text,
                content_hash=content_hash(text),
                fetched_at=fetched_at,
                dataset="warningshydro",
                published_at=parse_gov_date(published_raw),
                expires_at=expires_at,
                metadata={
                    "number": number,
                    "event": event,
                    "level": level,
                    "provinces": sorted(provinces),
                },
            )
        )

    return sort_latest(docs, limit=limit)


def parse_synop(
    payload: bytes | str,
    *,
    fetched_at: datetime,
    source_url: str = ENDPOINT_URLS["synop"],
    limit: int | None = 15,
    major_cities_first: bool = True,
) -> list[ImgwDocument]:
    if limit is not None:
        if limit < 0:
            raise ValueError("limit nie może być ujemny")
        if limit == 0:
            return []

    items = _decode_json_list(payload)
    docs: list[ImgwDocument] = []
    for item in items:
        station = _pick_first(item, "stacja")
        station_id = _pick_first(item, "id_stacji")
        if not station:
            continue
        date_str = _pick_first(item, "data_pomiaru")
        hour_str = _pick_first(item, "godzina_pomiaru")
        published_raw = f"{date_str} {hour_str.zfill(2)}:00" if date_str and hour_str else date_str

        temp = _pick_first(item, "temperatura")
        wind = _pick_first(item, "predkosc_wiatru")
        wind_dir = _pick_first(item, "kierunek_wiatru")
        humidity = _pick_first(item, "wilgotnosc_wzgledna")
        rain = _pick_first(item, "suma_opadu")
        pressure = _pick_first(item, "cisnienie")

        title = f"Pomiar meteorologiczny IMGW – stacja {station} ({published_raw or 'bieżący'})"
        parts = [
            f"Dane synoptyczne IMGW-PIB ze stacji {station} (ID {station_id or 'brak'})"
            f" z dnia {published_raw or 'brak daty'}:"
        ]
        if temp:
            parts.append(f"temperatura powietrza {temp} °C;")
        if wind:
            parts.append(f"prędkość wiatru {wind} m/s (kierunek {wind_dir or 'brak'}°);")
        if humidity:
            parts.append(f"wilgotność względna {humidity}%;")
        if rain:
            parts.append(f"suma opadu {rain} mm;")
        if pressure:
            parts.append(f"ciśnienie atmosferyczne {pressure} hPa.")

        text = normalize_text(" ".join(parts))
        doc_url = f"{source_url}/id/{station_id}" if station_id else f"{source_url}#{station}"
        docs.append(
            ImgwDocument(
                url=doc_url,
                title=title,
                text=text,
                content_hash=content_hash(text),
                fetched_at=fetched_at,
                dataset="synop",
                published_at=parse_gov_date(published_raw),
                metadata={"station_id": station_id, "station": station},
            )
        )

    docs.sort(
        key=lambda doc: (
            doc.published_at is not None,
            doc.published_at or _MIN_UTC,
            1
            if (
                major_cities_first
                and str(doc.metadata.get("station", "")).lower() in MAJOR_SYNOP_STATIONS
            )
            else 0,
        ),
        reverse=True,
    )
    return docs[:limit] if limit is not None else docs


def parse_hydro(
    payload: bytes | str,
    *,
    fetched_at: datetime,
    source_url: str = ENDPOINT_URLS["hydro"],
    limit: int | None = 15,
) -> list[ImgwDocument]:
    """Parsuje dane ze stacji hydrologicznych (od najnowszych, z priorytetem przekroczeń)."""
    if limit is not None:
        if limit < 0:
            raise ValueError("limit nie może być ujemny")
        if limit == 0:
            return []

    items = _decode_json_list(payload)
    docs: list[ImgwDocument] = []
    for item in items:
        station = _pick_first(item, "stacja")
        station_id = _pick_first(item, "id_stacji")
        water_level = _pick_first(item, "stan_wody")
        if not station or not water_level:
            continue

        river = _pick_first(item, "rzeka")
        province = _pick_first(item, "wojewodztwo")
        measured_at = _pick_first(item, "stan_wody_data_pomiaru")
        warn_level = _pick_first(item, "stan_ostrzegawczy")
        alarm_level = _pick_first(item, "stan_alarmowy")
        water_temp = _pick_first(item, "temperatura_wody")

        lv = _to_float(water_level)
        wv = _to_float(warn_level)
        av = _to_float(alarm_level)
        status_note = "w normie"
        if lv is not None and av is not None and lv >= av:
            status_note = "PRZEKROCZONY STAN ALARMOWY"
        elif lv is not None and wv is not None and lv >= wv:
            status_note = "PRZEKROCZONY STAN OSTRZEGAWCZY"

        title = f"Stan wody IMGW – {station}{f' ({river})' if river else ''}: {water_level} cm"
        lines = [
            f"Pomiar hydrologiczny IMGW-PIB na stacji {station}"
            f"{f', rzeka {river}' if river else ''}"
            f"{f', woj. {province}' if province else ''}.",
            f"Stan wody: {water_level} cm (status: {status_note}, pomiar: {measured_at or 'brak daty'}).",
        ]
        if warn_level or alarm_level:
            lines.append(
                f"Progi: stan ostrzegawczy {warn_level or 'brak'} cm, stan alarmowy {alarm_level or 'brak'} cm."
            )
        if water_temp:
            lines.append(f"Temperatura wody: {water_temp} °C.")

        text = normalize_text("\n".join(lines))
        doc_url = f"{source_url}/id/{station_id}" if station_id else f"{source_url}#{station}"
        docs.append(
            ImgwDocument(
                url=doc_url,
                title=title,
                text=text,
                content_hash=content_hash(text),
                fetched_at=fetched_at,
                dataset="hydro",
                published_at=parse_gov_date(measured_at),
                metadata={
                    "station_id": station_id,
                    "station": station,
                    "river": river,
                    "province": province,
                    "status": status_note,
                },
            )
        )

    severity_rank = {
        "PRZEKROCZONY STAN ALARMOWY": 3,
        "PRZEKROCZONY STAN OSTRZEGAWCZY": 2,
        "w normie": 1,
    }
    docs.sort(
        key=lambda doc: (
            doc.published_at is not None,
            doc.published_at or _MIN_UTC,
            severity_rank.get(str(doc.metadata.get("status", "")), 0),
        ),
        reverse=True,
    )
    return docs[:limit] if limit is not None else docs


class ImgwParser:
    """Konektor do otwartego API IMGW-PIB."""

    def __init__(self, fetcher: ImgwResourceFetcher) -> None:
        self.fetcher = fetcher

    def fetch_warnings_meteo(self, *, limit: int | None = None) -> list[ImgwDocument]:
        res = self.fetcher.get(ENDPOINT_URLS["warningsmeteo"])
        return parse_warnings_meteo(
            res.content, fetched_at=res.fetched_at, source_url=res.url, limit=limit
        )

    def fetch_warnings_hydro(self, *, limit: int | None = None) -> list[ImgwDocument]:
        res = self.fetcher.get(ENDPOINT_URLS["warningshydro"])
        return parse_warnings_hydro(
            res.content, fetched_at=res.fetched_at, source_url=res.url, limit=limit
        )

    def fetch_synop(self, *, limit: int | None = 10) -> list[ImgwDocument]:
        res = self.fetcher.get(ENDPOINT_URLS["synop"])
        return parse_synop(res.content, fetched_at=res.fetched_at, source_url=res.url, limit=limit)

    def fetch_hydro(self, *, limit: int | None = 10) -> list[ImgwDocument]:
        res = self.fetcher.get(ENDPOINT_URLS["hydro"])
        return parse_hydro(res.content, fetched_at=res.fetched_at, source_url=res.url, limit=limit)

    def fetch_latest(
        self,
        *,
        limit: int = 10,
        dataset: ImgwDataset | None = None,
    ) -> list[ImgwDocument]:
        """Pobiera do ``limit`` najnowszych dokumentów IMGW (z jednego zbioru lub łącznie)."""
        if limit < 0:
            raise ValueError("limit nie może być ujemny")
        if limit == 0:
            return []

        if dataset == "warningsmeteo":
            return self.fetch_warnings_meteo(limit=limit)
        if dataset == "warningshydro":
            return self.fetch_warnings_hydro(limit=limit)
        if dataset == "synop":
            return self.fetch_synop(limit=limit)
        if dataset == "hydro":
            return self.fetch_hydro(limit=limit)

        combined = self.fetch_all(
            warnings_limit=limit,
            synop_limit=limit,
            hydro_limit=limit,
        )
        combined.sort(
            key=lambda doc: (
                doc.published_at is not None,
                doc.published_at or _MIN_UTC,
                1 if doc.dataset in {"warningsmeteo", "warningshydro"} else 0,
            ),
            reverse=True,
        )
        return combined[:limit]

    def fetch_all(
        self,
        *,
        warnings_limit: int | None = 20,
        synop_limit: int | None = 10,
        hydro_limit: int | None = 10,
    ) -> list[ImgwDocument]:
        docs: list[ImgwDocument] = []
        for name, fn in (
            ("warningsmeteo", lambda: self.fetch_warnings_meteo(limit=warnings_limit)),
            ("warningshydro", lambda: self.fetch_warnings_hydro(limit=warnings_limit)),
            ("synop", lambda: self.fetch_synop(limit=synop_limit)),
            ("hydro", lambda: self.fetch_hydro(limit=hydro_limit)),
        ):
            try:
                docs.extend(fn())
            except Exception as exc:
                log.warning("IMGW: błąd pobierania zbioru %s: %s", name, exc)
                continue
        return docs
