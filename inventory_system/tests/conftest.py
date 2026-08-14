"""Shared fixtures for DB-backed integration tests (tests/models,
tests/repositories/test_sql_*).

``live_db`` is deliberately gated on a SEPARATE setting,
``INVENTORY_TEST_DATABASE_URL`` — never on ``INVENTORY_DATABASE_URL``, the
app's real database. Repository classes like SqlUserRepository/
SqlProductRepository talk to the database via app.database.session's
module-global engine, which reads settings.database_url — not a
test-local session — so a naive "create tables, run test, drop tables"
fixture pointed at the app's real database_url would create and then
DESTROY whatever schema/data already lives there the moment these tests
ran (a real risk once a persistent, non-throwaway Postgres is in use, as
opposed to a scratch instance). Requiring an explicit, distinct env var
makes that impossible by default: these tests simply skip unless a
developer deliberately opts a *test* database in.

To run these tests locally against a scratch database:

    createdb inventory_test
    export INVENTORY_TEST_DATABASE_URL=postgresql+psycopg://localhost/inventory_test
    pytest tests/models tests/repositories
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError

import app.database.session as db_session_module
from app.config.settings import settings
from app.models import Base


@pytest.fixture()
def live_db(monkeypatch):
    if not settings.test_database_url:
        pytest.skip("INVENTORY_TEST_DATABASE_URL is not set — see tests/conftest.py")

    test_engine = create_engine(settings.test_database_url, future=True)
    try:
        with test_engine.connect():
            pass
    except OperationalError:
        test_engine.dispose()
        pytest.skip(f"cannot connect to {settings.test_database_url} — "
                    "see tests/conftest.py")

    # Redirects app.database.session.get_session() (and therefore every
    # Sql*Repository) at the test database for the duration of this test.
    # monkeypatch reverts both after this fixture's teardown runs below.
    monkeypatch.setattr(settings, "database_url", settings.test_database_url)
    monkeypatch.setattr(db_session_module, "_engine", None)
    monkeypatch.setattr(db_session_module, "_SessionLocal", None)

    Base.metadata.create_all(test_engine)
    try:
        yield test_engine
    finally:
        Base.metadata.drop_all(test_engine)
        test_engine.dispose()
