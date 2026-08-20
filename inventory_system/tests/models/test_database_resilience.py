"""Corrupted/missing-database scenarios — a gap called out explicitly in
the production-hardening pass: nothing previously exercised what happens
when the database is unreachable, a table is missing (schema drift), or
the connection drops mid-transaction. These prove get_session() (app/
database/session.py) and the repositories built on it fail *safely* —
a clean, catchable exception and no partial writes — rather than hanging
or silently corrupting state.

Uses the ``live_db`` fixture (tests/conftest.py) where a real scratch
database is needed — see its docstring for why this is gated on
INVENTORY_TEST_DATABASE_URL, separate from the app's real
INVENTORY_DATABASE_URL, and how to run these locally. Tests that only need
an *unreachable* database don't use live_db at all (there's nothing to
connect to), and are always safe to run.
"""
import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError, ProgrammingError

import app.database.session as db_session_module
from app.config.settings import settings
from app.database.session import get_session
from app.models import Organization, Product


# -- unreachable database ---------------------------------------------- #

def test_get_session_raises_cleanly_when_database_unreachable(monkeypatch):
    # A bogus port on localhost — fails fast (connection refused) rather
    # than hanging on a routing timeout, so this test doesn't need a
    # network-level fault to actually be reliable/fast in CI.
    bad_url = "postgresql+psycopg://baduser:badpass@127.0.0.1:1/inventory_nonexistent"
    monkeypatch.setattr(settings, "database_url", bad_url)
    monkeypatch.setattr(db_session_module, "_engine", None)
    monkeypatch.setattr(db_session_module, "_SessionLocal", None)

    with pytest.raises(OperationalError):
        with get_session() as session:
            session.query(Organization).count()


def test_repository_call_against_unreachable_database_propagates_cleanly(monkeypatch):
    """Not just get_session() directly — a real repository built on top of
    it (SqlOrganizationRepository) must not mask, retry-forever, or return
    a falsely-empty result when the database can't be reached at all.
    """
    from app.repositories.sql.organization_repository import SqlOrganizationRepository

    bad_url = "postgresql+psycopg://baduser:badpass@127.0.0.1:1/inventory_nonexistent"
    monkeypatch.setattr(settings, "database_url", bad_url)
    monkeypatch.setattr(db_session_module, "_engine", None)
    monkeypatch.setattr(db_session_module, "_SessionLocal", None)

    import uuid
    with pytest.raises(OperationalError):
        SqlOrganizationRepository().get_by_id(uuid.uuid4())


def test_database_recovers_after_url_is_fixed(monkeypatch, live_db):
    """An unreachable-database failure must not permanently wedge the
    module-level engine — once a valid database_url is restored, a fresh
    get_session() must work normally again. (live_db already points
    settings.database_url at a real scratch database; this test breaks it,
    confirms the failure, then repairs it and confirms recovery, all
    within the same process — proving _init()'s engine caching doesn't
    trap a dead connection forever.)
    """
    good_url = settings.database_url

    monkeypatch.setattr(settings, "database_url",
                        "postgresql+psycopg://baduser:badpass@127.0.0.1:1/nope")
    monkeypatch.setattr(db_session_module, "_engine", None)
    monkeypatch.setattr(db_session_module, "_SessionLocal", None)
    with pytest.raises(OperationalError):
        with get_session() as session:
            session.query(Organization).count()

    monkeypatch.setattr(settings, "database_url", good_url)
    monkeypatch.setattr(db_session_module, "_engine", None)
    monkeypatch.setattr(db_session_module, "_SessionLocal", None)
    with get_session() as session:
        assert session.query(Organization).count() >= 0  # must not raise


# -- missing table / schema drift --------------------------------------- #

def test_query_against_a_dropped_table_raises_programming_error(live_db):
    with get_session() as session:
        session.execute(text("DROP TABLE products CASCADE"))

    with pytest.raises(ProgrammingError):
        with get_session() as session:
            session.query(Product).count()

    # The failure must not leave get_session() itself unusable for
    # everything else — a query against a table that's still there works.
    with get_session() as session:
        assert session.query(Organization).count() >= 0


def test_repository_call_against_a_dropped_table_propagates_cleanly(live_db):
    """Same as above, but through a real repository (not a raw session
    query) — proves ProductService/SqlProductRepository would surface a
    schema-drift failure to the caller instead of masking it as
    "no products found".
    """
    from app.repositories.sql.product_repository import SqlProductRepository
    from app.schemas.product import ProductFilter

    with get_session() as session:
        session.execute(text("DROP TABLE products CASCADE"))

    import uuid
    with pytest.raises(ProgrammingError):
        SqlProductRepository().search(uuid.uuid4(), ProductFilter())


# -- connection dropped mid-transaction ---------------------------------- #

def test_connection_dropped_mid_transaction_persists_nothing(live_db):
    """Simulates a real dropped connection (Postgres restart, network
    blip, firewall idle timeout) by closing the raw DBAPI connection out
    from under an active Session mid-transaction, after a first write has
    already been flushed but not committed. Proves get_session()'s
    rollback-on-exception still protects atomicity even when the failure
    is a connection-level fault, not an application-raised error — a
    materially different code path than the RuntimeError case already
    covered by test_rollback_on_exception_persists_nothing.
    """
    with pytest.raises(Exception):  # noqa: B017 - driver-specific (OperationalError/InterfaceError)
        with get_session() as session:
            session.add(Organization(name="Partial Write Before Drop"))
            session.flush()  # exists in the uncommitted transaction

            # Yank the connection out from under the ORM session.
            session.connection().connection.close()

            session.add(Organization(name="Should Never Persist"))
            session.flush()  # now fails — the connection is dead

    with get_session() as session:
        assert session.query(Organization).filter_by(
            name="Partial Write Before Drop").first() is None
        assert session.query(Organization).filter_by(
            name="Should Never Persist").first() is None


# -- startup schema-version guard ---------------------------------------- #
# check_schema_version (app.database.schema_check) is the fix for a real
# production incident: a deployed database was one migration behind the
# code, and the first user action that touched the missing column crashed
# deep inside a repository call instead of failing clearly at startup. See
# SchemaVersionMismatchError's docstring.

def _reset_alembic_version_table(engine) -> None:
    # Base.metadata.drop_all (live_db's own teardown) doesn't touch
    # alembic_version — it isn't part of Base.metadata, alembic owns it
    # separately — so a stamp() from an earlier test in the same run would
    # otherwise leak into whichever of these three runs next. Called at the
    # start of each test below for order-independence.
    with engine.connect() as conn:
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
        conn.commit()


def test_check_schema_version_passes_when_database_is_at_head(live_db):
    from alembic.command import stamp
    from app.database.schema_check import _expected_head_revision, check_schema_version
    from app.database.schema_check import _PROJECT_ROOT
    from alembic.config import Config

    _reset_alembic_version_table(live_db)
    config = Config(str(_PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_PROJECT_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", str(live_db.url))
    stamp(config, _expected_head_revision())

    check_schema_version()  # no exception


def test_check_schema_version_raises_when_database_is_behind(live_db):
    from alembic.command import stamp
    from alembic.config import Config

    from app.database.schema_check import _PROJECT_ROOT
    from app.core.exceptions import SchemaVersionMismatchError
    from app.database.schema_check import check_schema_version

    _reset_alembic_version_table(live_db)
    config = Config(str(_PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_PROJECT_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", str(live_db.url))
    # One revision behind head — reproduces the exact incident this guard
    # exists for (missing invoices.overall_discount_amount).
    stamp(config, "65a248c62115")

    with pytest.raises(SchemaVersionMismatchError) as excinfo:
        check_schema_version()
    assert "65a248c62115" in str(excinfo.value)


def test_check_schema_version_raises_when_no_migrations_ever_applied(live_db):
    """live_db's own Base.metadata.create_all builds every table without
    ever writing an alembic_version row — the same state a brand-new
    database would be in before anyone runs ``alembic upgrade head``.
    """
    from app.core.exceptions import SchemaVersionMismatchError
    from app.database.schema_check import check_schema_version

    _reset_alembic_version_table(live_db)

    with pytest.raises(SchemaVersionMismatchError) as excinfo:
        check_schema_version()
    assert "no migrations applied" in str(excinfo.value)
