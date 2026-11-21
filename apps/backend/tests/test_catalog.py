from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from prawdowid.domain.enums import SourceKind
from prawdowid.ingestion.catalog import DEFAULT_CATALOG, Catalog, load_catalog, sync_catalog
from prawdowid.ingestion.collectors.registry import COLLECTORS
from prawdowid.persistence.models import Source


def entry(slug: str, **overrides):
    base = {
        "slug": slug,
        "name": "Test",
        "publisher": "Test publisher",
        "kind": "official",
        "domain": "example.gov.pl",
        "tier": 1,
        "license": "test",
        "inclusion_rule": "test",
        "collect": {"collector": "gov_pl", "static_urls": ["https://www.gov.pl/web/x"]},
    }
    return {**base, **overrides}


def test_repository_catalog_is_valid_and_meets_type_requirement() -> None:
    catalog = load_catalog(DEFAULT_CATALOG)
    assert len({e.kind for e in catalog.source}) >= 2
    assert {e.collect.collector for e in catalog.source} <= set(COLLECTORS)
    assert all(e.inclusion_rule and e.license for e in catalog.source)


def test_collect_params_are_the_extra_keys() -> None:
    catalog = Catalog.model_validate({"source": [entry("a")]})
    assert catalog.source[0].collect.params == {"static_urls": ["https://www.gov.pl/web/x"]}


@pytest.mark.parametrize(
    "bad",
    [
        {"domain": "https://gov.pl/web"},
        {"tier": 4},
        {"kind": "blog"},
        {"slug": "Has Spaces"},
        {"inclusion_rule": ""},
    ],
)
def test_invalid_entries_are_rejected(bad: dict) -> None:
    with pytest.raises(ValidationError):
        Catalog.model_validate({"source": [{**entry("a"), **bad}]})


def test_duplicate_slugs_are_rejected() -> None:
    with pytest.raises(ValidationError, match="Duplicate"):
        Catalog.model_validate({"source": [entry("a"), entry("a")]})


def test_load_reads_toml(tmp_path: Path) -> None:
    path = tmp_path / "catalog.toml"
    path.write_text(
        '[[source]]\nslug="x"\nname="X"\npublisher="X"\nkind="fact_checker"\ndomain="x.pl"\n'
        'tier=2\nlicense="l"\ninclusion_rule="r"\n[source.collect]\ncollector="google_fact_check"\n'
        'publisher_site="x.pl"\n',
        encoding="utf-8",
    )
    catalog = load_catalog(path)
    assert catalog.get("x").kind is SourceKind.FACT_CHECKER
    assert catalog.get("x").collect.params == {"publisher_site": "x.pl"}


def test_sync_upserts_and_deactivates_removed(isolated_sessions) -> None:
    with isolated_sessions() as session, session.begin():
        first = sync_catalog(session, Catalog.model_validate({"source": [entry("t-a"), entry("t-b")]}))
        assert first["added"] == 2
    with isolated_sessions() as session, session.begin():
        stats = sync_catalog(
            session, Catalog.model_validate({"source": [entry("t-a", name="Renamed")]})
        )
        assert stats["updated"] == 1 and stats["deactivated"] >= 1
        rows = {s.slug: s for s in session.scalars(select(Source).where(Source.slug.in_(["t-a", "t-b"])))}
        assert rows["t-a"].name == "Renamed" and rows["t-a"].active
        assert not rows["t-b"].active
    with isolated_sessions() as session, session.begin():
        again = sync_catalog(session, Catalog.model_validate({"source": [entry("t-a", name="Renamed")]}))
        assert again["added"] == 0 and again["updated"] == 0
