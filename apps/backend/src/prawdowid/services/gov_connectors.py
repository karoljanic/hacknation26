"""Decision step: which catalogued official sources to query live for a claim.

Google Fact Check and EUvsDisinfo are always queried (``evidence_search``).
This is the additional "which web APIs do we run" decision: for each claim, a
bounded Jev decision call picks which catalogued official sources are worth a
live fetch:

* ``gov_pl``: gov.pl sections (security, health, defence, infrastructure, ...),
  read with the same parser as batch ingestion;
* ``official_page``: news or information pages of other official services
  (Police, GDDKiA roads, aviation and rail authorities, maritime offices,
  CERT Polska, ...), read with the sanitising article extractor;
* ``imgw``: the IMGW-PIB open-data API (weather warnings, synoptic and
  hydrological measurements).

Their *current* content is folded into the same ``EvidenceItem`` shape used by
Google/EUvsDisinfo. A listing page or an API dump is long and mostly about
other things, so only the documents and passages sharing the most words with
the claim are kept (``services.relevance``).

A fetch failure for one selected source degrades gracefully (that source
contributes no evidence) and is reported back, never silently hidden and
never treated as "this topic has no official information".
"""

import json
import logging
import re
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from prawdowid.adapters.sources.gov_pl import GovPlHttpFetcher, GovPlParser
from prawdowid.adapters.sources.imgw import ImgwHttpFetcher, ImgwParser
from prawdowid.adapters.sources.web import PublicWebFetcher
from prawdowid.ai.adapters.decision import BooleanQuestion, DecisionAdapter
from prawdowid.domain.evidence import EvidenceItem, EvidenceSource
from prawdowid.ingestion.catalog import SourceEntry
from prawdowid.ingestion.collectors.gov_pl import GovPlCollector
from prawdowid.ingestion.domain import Skipped
from prawdowid.services.preparation import extract_document
from prawdowid.services.relevance import best_passages, score

SELECTION_THRESHOLD = 0.5
MAX_SOURCES_PER_CLAIM = 3
SNIPPET_CHARS = 800
MAX_ITEMS_PER_SOURCE = 4
# A listing with no document matching the claim's words still shows its
# newest entries (the claim may use other words); a measurement API does not.
FALLBACK_ITEMS = 2
MAX_PAGE_CHARS = 60_000

CONNECTOR_COLLECTORS = frozenset({"gov_pl", "official_page", "imgw"})
_SOURCE_BY_COLLECTOR: dict[str, EvidenceSource] = {
    "gov_pl": "gov_pl",
    "official_page": "official_site",
    "imgw": "imgw",
}

SELECTION_INSTRUCTIONS = (
    "Oficjalne źródło: {name} ({publisher}).\n"
    "Zakres tego źródła: {inclusion_rule}\n"
    "Czy teza poniżej dotyczy tematyki obsługiwanej przez to konkretne źródło "
    "na tyle, że warto sprawdzić jego najnowsze, aktualne komunikaty lub dane? "
    "Samo ogólne skojarzenie z administracją publiczną to za mało — źródło "
    "musi realnie dotyczyć tematu tezy (np. pogoda, drogi, zdrowie, obronność)."
)

log = logging.getLogger(__name__)

# IMGW synoptic stations are named after their town; a claim names it inflected
# ("w Toruniu", "w Łodzi"), so names are compared without diacritics, by prefix.
SYNOP_TITLE = re.compile(r"stacja (?P<name>.+?) \(")
MAX_STATIONS_PER_CLAIM = 2
STATION_ALIASES = {"Białystok": ("bialymstok",)}
# Not a town: an offshore platform ("Platforma Obywatelska" would match it).
IGNORED_STATIONS = {"Platforma"}
# Large towns without a synoptic station: the nearest one, said so in the title.
NEAREST_STATION = {
    "bydgoszcz": "Toruń",
    "gdynia": "Gdańsk",
    "sopot": "Gdańsk",
    "radom": "Kozienice",
    "gliwice": "Katowice",
    "zabrze": "Katowice",
    "bytom": "Katowice",
    "sosnowiec": "Katowice",
    "tychy": "Katowice",
    "rybnik": "Racibórz",
}


def _plain(text: str) -> str:
    text = text.lower().replace("ł", "l")
    return "".join(ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch))


def _place_words(words: list[str], name: str) -> set[int]:
    """Positions of the words naming the place (each word of its name, possibly
    inflected); empty when any word of the name is missing."""
    found: set[int] = set()
    for part in re.split(r"[\s-]+", _plain(name)):
        # The "e" of "-ek" drops when inflected: Szczecinek -> w Szczecinku.
        stem = part[:-2] + "k" if part.endswith("ek") else part[: max(3, len(part) - 2)]
        hits = [i for i, word in enumerate(words) if word.startswith(stem) and len(word) <= len(part) + 4]
        if not hits:
            return set()
        found.update(hits)
    return found


class GovConnectorSelector:
    """Picks a bounded subset of catalogued official sources worth a live fetch
    for one claim. Decision-only: it never fetches anything itself."""

    def __init__(
        self,
        *,
        decision: DecisionAdapter,
        entries: Sequence[SourceEntry],
        threshold: float = SELECTION_THRESHOLD,
        max_sources: int = MAX_SOURCES_PER_CLAIM,
    ) -> None:
        if not entries:
            raise ValueError("At least one catalogued official source is required")
        if max_sources < 1:
            raise ValueError("max_sources must be positive")
        self.decision = decision
        self.entries = list(entries)
        self.threshold = threshold
        self.max_sources = max_sources

    def select(self, claim_text: str) -> list[SourceEntry]:
        questions = {
            str(index): BooleanQuestion(
                instructions=SELECTION_INSTRUCTIONS.format(
                    name=entry.name, publisher=entry.publisher, inclusion_rule=entry.inclusion_rule
                )
            )
            for index, entry in enumerate(self.entries)
        }
        state = json.dumps(
            {"claim": claim_text, "sources": [entry.name for entry in self.entries]}, ensure_ascii=False
        )
        result = self.decision.decide(state, questions)
        ranked = sorted(range(len(self.entries)), key=lambda i: result.answers[str(i)].noul, reverse=True)  # type: ignore[union-attr]
        selected = [i for i in ranked if result.answers[str(i)].noul > self.threshold][: self.max_sources]  # type: ignore[union-attr]
        return [self.entries[i] for i in selected]


@dataclass(frozen=True)
class GovFetchResult:
    items: list[EvidenceItem]
    failed_slugs: list[str]


@dataclass(frozen=True)
class _Document:
    title: str
    url: str
    text: str
    published_at: datetime | None = None


SourceDocument = _Document


def read_official_page(url: str, domain: str) -> _Document:
    """One official page, fetched only from the catalogued host."""
    article = PublicWebFetcher().fetch(url, allowed_hosts={domain, f"www.{domain}"})
    title, texts = extract_document(article, visible_text_fallback=True)
    return _Document(title=title or url, url=article.url, text="\n".join(texts)[:MAX_PAGE_CHARS])


def read_imgw() -> list[_Document]:
    with ImgwHttpFetcher() as fetcher:
        documents = ImgwParser(fetcher).fetch_all(warnings_limit=None, synop_limit=None, hydro_limit=None)
    return [
        _Document(title=document.title, url=document.url, text=document.text, published_at=document.published_at)
        for document in documents
    ]


class GovConnectorFetcher:
    """Live-fetches the *current* content of selected catalogued official
    sources and keeps the documents and passages about the claim."""

    def __init__(
        self,
        *,
        page_reader: Callable[[str, str], _Document] = read_official_page,
        imgw_reader: Callable[[], list[_Document]] = read_imgw,
    ) -> None:
        self.page_reader = page_reader
        self.imgw_reader = imgw_reader

    def fetch_many(
        self, entries: Sequence[SourceEntry], wanted: set[str] | frozenset[str] = frozenset(), claim_text: str = ""
    ) -> GovFetchResult:
        items: list[EvidenceItem] = []
        failed: list[str] = []
        for entry in entries:
            try:
                items.extend(self._fetch_one(entry, set(wanted), claim_text))
            except Exception:
                log.warning("Live fetch failed for %s", entry.slug, exc_info=True)
                failed.append(entry.slug)
        return GovFetchResult(items=items, failed_slugs=failed)

    def _fetch_one(self, entry: SourceEntry, wanted: set[str], claim_text: str) -> list[EvidenceItem]:
        collector = entry.collect.collector
        if collector == "gov_pl":
            documents = _read_gov_pl(entry)
        elif collector == "official_page":
            documents = [self.page_reader(url, entry.domain) for url in entry.collect.params.get("urls", [])]
        elif collector == "imgw":
            documents = self.imgw_reader()
            # The measurement from the station the claim names comes first:
            # warnings share more words with "czy pada w ..." and would push it out.
            stations = _station_measurements(documents, claim_text)
            shown = {document.url for document in stations}
            rest = [document for document in documents if document.url not in shown]
            return _items(entry, [(document, document.text) for document in stations]) + _focused_items(
                entry, rest, wanted, fallback=False, limit=MAX_ITEMS_PER_SOURCE - len(stations)
            )
        else:
            raise ValueError(f"No live connector for collector {collector!r}")
        return _focused_items(entry, documents, wanted, fallback=True)


def _read_gov_pl(entry: SourceEntry) -> list[_Document]:
    documents: list[_Document] = []
    with GovPlHttpFetcher() as fetcher:
        for result in GovPlCollector(GovPlParser(fetcher)).collect(entry.collect.params):
            if isinstance(result, Skipped):
                continue
            documents.append(_Document(result.title, result.url, result.text, result.published_at))
    return documents


def _station_measurements(documents: Sequence[_Document], claim_text: str) -> list[_Document]:
    """Current synoptic measurements from the stations (towns) the claim names,
    or from the nearest station for a large town without one."""
    words = re.findall(r"\w+", _plain(claim_text))
    if not words:
        return []
    synop: dict[str, _Document] = {}
    for document in documents:
        match = SYNOP_TITLE.search(document.title) if "/synop/" in document.url else None
        if match and match.group("name") not in IGNORED_STATIONS:
            synop[match.group("name")] = document
    matched: dict[str, set[int]] = {}
    for name in synop:
        positions = _place_words(words, name) or {
            i for i, word in enumerate(words) for alias in STATION_ALIASES.get(name, ()) if word.startswith(alias)
        }
        if positions:
            matched[name] = positions
    # "w Zielonej Górze" also reads as "Gorzów": a name matched only by words
    # of a longer matched name is dropped.
    named = [
        name for name, positions in matched.items()
        if not any(other != name and positions < other_positions for other, other_positions in matched.items())
    ]
    chosen = [synop[name] for name in sorted(named, key=lambda name: -len(matched[name]))]
    for town, station in NEAREST_STATION.items():
        if station in synop and _place_words(words, town) and synop[station] not in chosen:
            nearest = synop[station]
            chosen.append(
                _Document(
                    title=f"{nearest.title} – najbliższa stacja synoptyczna IMGW dla miejscowości {town.capitalize()}",
                    url=nearest.url, text=nearest.text, published_at=nearest.published_at,
                )
            )
    return chosen[:MAX_STATIONS_PER_CLAIM]


def _focused_items(
    entry: SourceEntry, documents: Sequence[_Document], wanted: set[str], *, fallback: bool,
    limit: int = MAX_ITEMS_PER_SOURCE,
) -> list[EvidenceItem]:
    """The documents sharing the most words with the claim, each cut to its
    best-matching passages; or, for listings, the newest entries if none match."""
    ranked = sorted(
        ((score(f"{document.title} {document.text}", wanted), index, document) for index, document in enumerate(documents)),
        key=lambda item: (-item[0], item[1]),
    )
    matching = [(document, best_passages(document.text, wanted)) for points, _, document in ranked if points > 0]
    chosen = [(document, passages or document.text[:SNIPPET_CHARS]) for document, passages in matching][: max(0, limit)]
    if not chosen and fallback:
        chosen = [(document, document.text[:SNIPPET_CHARS]) for document in documents[:FALLBACK_ITEMS]]
    return _items(entry, chosen)


def _items(entry: SourceEntry, chosen: Sequence[tuple[_Document, str]]) -> list[EvidenceItem]:
    return [
        EvidenceItem(
            source=_SOURCE_BY_COLLECTOR[entry.collect.collector],
            kind="official",
            title=document.title,
            url=document.url,
            publisher=entry.publisher,
            catalog_slug=entry.slug,
            snippet=snippet or document.title,
            published_at=document.published_at,
            lang="pl",
        )
        for document, snippet in chosen
    ]
