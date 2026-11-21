from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from prawdowid.persistence.db import get_engine

BACKEND = Path(__file__).resolve().parents[1]


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
