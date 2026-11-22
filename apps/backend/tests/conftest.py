from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from prawdowid.persistence.db import get_engine
from prawdowid.security.injection_guard import InjectionGuard, get_injection_guard_factory

BACKEND = Path(__file__).resolve().parents[1]


class StubDetector:
    """Offline stand-in for JEV / Guardrails: returns ``next_score``, or
    raises ``error`` when set, and records what it scored."""

    def __init__(self, name: str = "stub", threshold: float = 0.5, score: float = 0.0) -> None:
        self.name = name
        self.threshold = threshold
        self.next_score = score
        self.error: Exception | None = None
        self.scored: list[str] = []

    def score(self, text: str) -> float:
        self.scored.append(text)
        if self.error is not None:
            raise self.error
        return self.next_score


@pytest.fixture(autouse=True)
def injection_guard() -> Iterator[StubDetector]:
    """Every API test gets the real InjectionGuard with one stub detector that
    lets text through, so no test needs the models or the TypeSafe API; tests
    of the guard change the detector's ``next_score`` or ``error``."""
    from prawdowid.api.app import app

    detector = StubDetector()
    guard = InjectionGuard([detector])
    app.dependency_overrides[get_injection_guard_factory] = lambda: (lambda: guard)
    yield detector
    app.dependency_overrides.pop(get_injection_guard_factory, None)


@pytest.fixture(scope="session")
def migrated_db() -> None:
    try:
        with get_engine().connect() as connection:
            connection.execute(text("SELECT 1"))
    except OperationalError:
        pytest.skip("PostgreSQL not reachable (run `dev-services up -d`)")
    command.upgrade(Config(str(BACKEND / "alembic.ini")), "head")


@pytest.fixture
def isolated_sessions(migrated_db: None) -> Iterator[Callable[[], Session]]:
    """Session factory whose commits are savepoints inside one outer transaction
    that is rolled back after the test, so the dev database is never modified."""
    connection = get_engine().connect()
    outer = connection.begin()
    factory = sessionmaker(
        bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    try:
        yield factory
    finally:
        outer.rollback()
        connection.close()
