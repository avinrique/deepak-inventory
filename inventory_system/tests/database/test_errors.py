"""app.database.errors — the classification a user actually reads.

The bug this replaces was specific and damaging: schema_check caught
OperationalError while probing for the alembic_version table, so "the
server is unreachable", "the password is wrong" and "the port is blocked"
all reached the user as *"Database schema has no migrations applied. Run:
alembic upgrade head"*. These tests pin each cause to its own message.
"""
from decimal import Decimal

import pytest
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from app.core.exceptions import (
    AppError,
    DatabaseAuthenticationError,
    DatabaseTimeoutError,
    DatabaseUnavailableError,
    InsufficientStockError,
)
from app.database.errors import translate_db_error, user_message


def _operational(message: str, sqlstate: str | None = None) -> OperationalError:
    original = Exception(message)
    if sqlstate:
        original.sqlstate = sqlstate
    return OperationalError("SELECT 1", {}, original)


@pytest.mark.parametrize("sqlstate,expected", [
    ("28P01", DatabaseAuthenticationError),   # invalid password
    ("28000", DatabaseAuthenticationError),   # invalid authorization
    ("3D000", DatabaseAuthenticationError),   # database does not exist
    ("53300", DatabaseUnavailableError),      # too many connections
    ("57P03", DatabaseTimeoutError),          # cannot connect now / starting up
])
def test_sqlstate_drives_the_classification(sqlstate, expected):
    assert isinstance(translate_db_error(_operational("nope", sqlstate)), expected)


@pytest.mark.parametrize("message,expected", [
    ("could not connect to server: Connection refused", DatabaseUnavailableError),
    ('could not translate host name "db.example" to address', DatabaseUnavailableError),
    ("Network is unreachable", DatabaseUnavailableError),
    ("connection timeout expired", DatabaseTimeoutError),
    ("server closed the connection unexpectedly", DatabaseUnavailableError),
])
def test_message_patterns_classify_connections_with_no_sqlstate(message, expected):
    """A connection that never reached the server has no SQLSTATE to offer,
    which is exactly the offline-laptop case."""
    assert isinstance(translate_db_error(_operational(message)), expected)


def test_every_translated_error_is_an_apperror():
    """The UI's error paths dispatch on AppError; anything outside that
    hierarchy falls through to a generic "something went wrong"."""
    assert isinstance(translate_db_error(_operational("boom")), AppError)


def test_the_headline_never_contains_driver_noise():
    exc = _operational("FATAL: password authentication failed for user \"bob\"", "28P01")

    message = str(translate_db_error(exc))

    assert "OperationalError" not in message
    assert "FATAL" not in message
    assert "sqlalchemy" not in message.lower()
    assert "Settings" in message  # tells them where to go


def test_the_driver_text_is_kept_for_the_log_and_details_pane():
    exc = _operational("FATAL: password authentication failed", "28P01")

    assert "password authentication failed" in translate_db_error(exc).detail


def test_an_application_error_passes_through_unchanged():
    """A service that raised InsufficientStockError must not be relabelled
    as a network fault just because it travelled through a database call."""
    original = InsufficientStockError("product-1", "warehouse-1",
                                      available=Decimal("2"), requested=Decimal("5"))

    assert translate_db_error(original) is original


def test_a_non_database_exception_is_re_raised_not_mislabelled():
    """A TypeError is a bug in our code, not a connection problem; hiding it
    behind "check your network" would send the user chasing nothing."""
    with pytest.raises(TypeError):
        translate_db_error(TypeError("not a database problem"))


def test_user_message_is_safe_for_any_exception():
    """Used directly by UI error slots, which receive whatever a Worker
    raised — it must never itself raise."""
    assert user_message(TypeError("bug")) == "Something went wrong. Please try again."
    assert "database" in user_message(SQLAlchemyError("boom")).lower()
