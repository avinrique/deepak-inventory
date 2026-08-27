"""The specific regression that motivated Phase 3.

check_schema_version() probes for the alembic_version table. That probe
must treat "the table isn't there" and "I couldn't reach the server" as
different things — the old code caught OperationalError alongside
ProgrammingError, so every connection failure was reported to the user as
"Database schema has no migrations applied. Run: alembic upgrade head".
"""
import pytest
from sqlalchemy.exc import OperationalError, ProgrammingError

from app.core.exceptions import DatabaseError, SchemaVersionMismatchError
from app.database import schema_check


@pytest.fixture()
def head(monkeypatch):
    monkeypatch.setattr(schema_check, "_expected_head_revision", lambda: "abc123")
    return "abc123"


def _raise(exc):
    def _fail(*_args, **_kwargs):
        raise exc
    return _fail


def _operational() -> OperationalError:
    return OperationalError("SELECT 1", {}, Exception("could not connect to server"))


def _undefined_table() -> ProgrammingError:
    return ProgrammingError(
        "SELECT 1", {}, Exception('relation "alembic_version" does not exist'))


def test_an_unreachable_database_is_not_reported_as_schema_drift(head, monkeypatch):
    monkeypatch.setattr(schema_check, "get_session", _raise(_operational()))

    with pytest.raises(DatabaseError) as caught:
        schema_check.check_schema_version()

    assert not isinstance(caught.value, SchemaVersionMismatchError)
    assert "alembic" not in str(caught.value).lower()
    assert "reached" in str(caught.value).lower()


def test_a_missing_alembic_version_table_still_means_an_empty_database(head, monkeypatch):
    """The legitimate reason to swallow an error here: a brand-new database
    genuinely has no alembic_version table yet."""
    monkeypatch.setattr(schema_check, "get_session", _raise(_undefined_table()))

    with pytest.raises(SchemaVersionMismatchError) as caught:
        schema_check.check_schema_version()

    assert caught.value.actual is None
    assert caught.value.expected == "abc123"


def test_database_is_empty_reports_true_for_a_fresh_database(head, monkeypatch):
    monkeypatch.setattr(schema_check, "get_session", _raise(_undefined_table()))

    assert schema_check.database_is_empty() is True


def test_database_is_empty_propagates_a_connection_failure(head, monkeypatch):
    """The setup wizard calls this to decide whether to offer "Set up this
    database" — answering "yes, it's empty" for a server it never reached
    would offer to migrate a database that might be full of data."""
    monkeypatch.setattr(schema_check, "get_session", _raise(_operational()))

    with pytest.raises(DatabaseError):
        schema_check.database_is_empty()
