"""Build collectors by catalogue name. New sources register here."""

import os
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager

import httpx

from prawdowid.ingestion.domain import Collector, CollectorUnavailable


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise CollectorUnavailable(f"{name} is not set")
    return value


@contextmanager
def _gov_pl() -> Iterator[Collector]:
    from prawdowid.adapters.sources.gov_pl import GovPlHttpFetcher, GovPlParser
    from prawdowid.ingestion.collectors.gov_pl import GovPlCollector

    with GovPlHttpFetcher() as fetcher:
        yield GovPlCollector(GovPlParser(fetcher))


@contextmanager
def _google_fact_check() -> Iterator[Collector]:
    from prawdowid.adapters.sources.google_fact_check import GoogleFactCheckAdapter
    from prawdowid.ingestion.collectors.google_fact_check import GoogleFactCheckCollector

    key = _require_env("FACTCHECK_API_KEY")
    with httpx.Client() as client:
        yield GoogleFactCheckCollector(GoogleFactCheckAdapter(client=client, api_key=key))


@contextmanager
def _euvsdisinfo() -> Iterator[Collector]:
    from prawdowid.adapters.sources.euvsdisinfo import EUvsDisinfoAdapter
    from prawdowid.domain.euvsdisinfo import EUvsDisinfoConnection
    from prawdowid.ingestion.collectors.euvsdisinfo import EUvsDisinfoCollector

    connection = EUvsDisinfoConnection(api_key=_require_env("EUVSDISINFO_API_KEY"))
    yield EUvsDisinfoCollector(lambda: EUvsDisinfoAdapter.connect(connection))


@contextmanager
def _google_fact_check_live() -> Iterator[Collector]:
    """Catalogued only so live POST /check citations (any publisher, not just
    the five named redactions above) have an auditable Source to attach to.
    Not a batch collector: there is nothing to sweep without a specific claim."""
    raise CollectorUnavailable(
        "google-fact-check-live has no batch collector; it only attributes live /check citations"
    )
    yield  # pragma: no cover - unreachable, keeps this a generator function


COLLECTORS: dict[str, Callable[[], AbstractContextManager[Collector]]] = {
    "gov_pl": _gov_pl,
    "google_fact_check": _google_fact_check,
    "euvsdisinfo": _euvsdisinfo,
    "google_fact_check_live": _google_fact_check_live,
}


def open_collector(name: str) -> AbstractContextManager[Collector]:
    """Context manager yielding a ready collector.

    Raises ``KeyError`` for an unknown name and ``CollectorUnavailable`` when the
    collector cannot run here (e.g. its API key is missing).
    """
    try:
        factory = COLLECTORS[name]
    except KeyError:
        raise KeyError(f"Unknown collector '{name}'; known: {sorted(COLLECTORS)}") from None
    return factory()
