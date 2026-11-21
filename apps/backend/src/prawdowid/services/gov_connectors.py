"""Decision step: which catalogued gov.pl sources to query live for a claim.

Google Fact Check and EUvsDisinfo are always queried (``evidence_search``).
This is the additional "which web APIs do we run" decision: for each claim, a
bounded Jev decision call picks which of the catalogued official gov.pl
sections (security, health, weather, infrastructure, defence, ...) are worth a
live fetch, then fetches each one's *current* listing/static page and folds
the results into the same ``EvidenceItem`` shape used by Google/EUvsDisinfo,
so sublink selection and assessment do not need to know the difference.

A fetch failure for one selected source degrades gracefully (that source
contributes no evidence) and is reported back, never silently hidden and
never treated as "this topic has no official information".
"""

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass

from prawdowid.adapters.sources.gov_pl import GovPlHttpFetcher, GovPlParser
from prawdowid.ai.adapters.decision import BooleanQuestion, DecisionAdapter
from prawdowid.domain.evidence import EvidenceItem
from prawdowid.ingestion.catalog import SourceEntry
from prawdowid.ingestion.collectors.gov_pl import GovPlCollector
from prawdowid.ingestion.domain import CorpusDocument, Skipped

SELECTION_THRESHOLD = 0.5
MAX_SOURCES_PER_CLAIM = 3
SNIPPET_CHARS = 800

SELECTION_INSTRUCTIONS = (
    "Oficjalne źródło rządowe: {name} ({publisher}).\n"
    "Zakres tego źródła: {inclusion_rule}\n"
    "Czy teza poniżej dotyczy tematyki obsługiwanej przez to konkretne źródło "
    "na tyle, że warto sprawdzić jego najnowsze, aktualne komunikaty? "
    "Samo ogólne skojarzenie z administracją rządową to za mało — źródło "
    "musi realnie dotyczyć tematu tezy (np. pogoda, drogi, zdrowie, obronność)."
)

log = logging.getLogger(__name__)


class GovConnectorSelector:
    """Picks a bounded subset of catalogued gov.pl entries worth a live fetch
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
            raise ValueError("At least one gov.pl catalogue entry is required")
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


class GovConnectorFetcher:
    """Live-fetches the *current* listing/static page(s) of a selected
    catalogued gov.pl source. Uses the exact same parser as batch ingestion,
    just called synchronously for one source instead of swept in bulk."""

    def fetch_many(self, entries: Sequence[SourceEntry]) -> GovFetchResult:
        items: list[EvidenceItem] = []
        failed: list[str] = []
        for entry in entries:
            try:
                items.extend(self._fetch_one(entry))
            except Exception:
                log.warning("Live gov.pl fetch failed for %s", entry.slug, exc_info=True)
                failed.append(entry.slug)
        return GovFetchResult(items=items, failed_slugs=failed)

    def _fetch_one(self, entry: SourceEntry) -> list[EvidenceItem]:
        items: list[EvidenceItem] = []
        with GovPlHttpFetcher() as fetcher:
            collector = GovPlCollector(GovPlParser(fetcher))
            for result in collector.collect(entry.collect.params):
                if isinstance(result, Skipped):
                    continue
                items.append(_to_evidence_item(entry, result))
        return items


def _to_evidence_item(entry: SourceEntry, doc: CorpusDocument) -> EvidenceItem:
    return EvidenceItem(
        source="gov_pl",
        kind="official",
        title=doc.title,
        url=doc.url,
        publisher=entry.publisher,
        catalog_slug=entry.slug,
        snippet=doc.text[:SNIPPET_CHARS] or doc.title,
        published_at=doc.published_at,
        lang="pl",
    )
