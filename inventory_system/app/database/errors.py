"""Turns driver-level database failures into messages a shop owner can act on.

The UI must never show "sqlalchemy.exc.OperationalError: connection to
server at ... failed: FATAL: password authentication failed for user". It
must say which of a small number of things went wrong and what to do about
it, because those remedies are genuinely different: check the network, fix
the credentials, wait and retry, or call an administrator.

Classification is by SQLSTATE first (psycopg exposes it on the wrapped
exception) and by message pattern only as a fallback, because a connection
that never reached the server has no SQLSTATE to offer.
"""
import logging
import re

from sqlalchemy.exc import DBAPIError, OperationalError, SQLAlchemyError

from app.core.exceptions import (
    AppError,
    DatabaseAuthenticationError,
    DatabaseTimeoutError,
    DatabaseUnavailableError,
)

_logger = logging.getLogger(__name__)

# https://www.postgresql.org/docs/current/errcodes-appendix.html
_INVALID_PASSWORD = "28P01"
_INVALID_AUTHORIZATION = "28000"
_UNDEFINED_DATABASE = "3D000"
_TOO_MANY_CONNECTIONS = "53300"
_CANNOT_CONNECT_NOW = "57P03"

_TIMEOUT_PATTERN = re.compile(r"timeout|timed out", re.IGNORECASE)
_UNREACHABLE_PATTERN = re.compile(
    r"could not connect|connection refused|no route to host|network is unreachable|"
    r"could not translate host name|name or service not known|nodename nor servname|"
    r"temporary failure in name resolution|server closed the connection|"
    r"connection reset|is the server running",
    re.IGNORECASE)
_SSL_PATTERN = re.compile(r"ssl|certificate", re.IGNORECASE)


def _sqlstate(exc: BaseException) -> str | None:
    original = getattr(exc, "orig", None)
    for candidate in (original, exc):
        code = getattr(candidate, "sqlstate", None) or getattr(candidate, "pgcode", None)
        if code:
            return str(code)
    return None


def translate_db_error(exc: BaseException) -> AppError:
    """Maps a driver exception to the AppError the UI should present.

    Anything already an AppError is passed straight through — a service that
    raised InsufficientStockError must not be relabelled as a network fault
    just because it travelled through a database call.
    """
    if isinstance(exc, AppError):
        return exc

    detail = str(exc)
    state = _sqlstate(exc)

    if state in (_INVALID_PASSWORD, _INVALID_AUTHORIZATION):
        return DatabaseAuthenticationError(
            "The database rejected the username or password.\n\n"
            "Check the credentials in Settings → Database.", detail)
    if state == _UNDEFINED_DATABASE:
        return DatabaseAuthenticationError(
            "That database does not exist on the server.\n\n"
            "Check the database name in Settings → Database.", detail)
    if state == _TOO_MANY_CONNECTIONS:
        return DatabaseUnavailableError(
            "The database server has too many connections open right now.\n\n"
            "Try again in a moment, or ask your administrator to look at it.", detail)
    if state == _CANNOT_CONNECT_NOW:
        return DatabaseTimeoutError(
            "The database server is starting up and cannot accept connections yet.\n\n"
            "Wait a few seconds and try again.", detail)

    if isinstance(exc, (OperationalError, DBAPIError, OSError)):
        if _TIMEOUT_PATTERN.search(detail):
            return DatabaseTimeoutError(
                "The database did not respond in time.\n\n"
                "It may be starting up, or the connection may be slow. "
                "Try again in a moment.", detail)
        if _SSL_PATTERN.search(detail) and not _UNREACHABLE_PATTERN.search(detail):
            return DatabaseUnavailableError(
                "A secure connection to the database could not be established.\n\n"
                "Check the SSL setting in Settings → Database.", detail)
        if _UNREACHABLE_PATTERN.search(detail):
            return DatabaseUnavailableError(
                "The database server could not be reached.\n\n"
                "Check that this computer is online and that the server address "
                "in Settings → Database is correct.", detail)
        return DatabaseUnavailableError(
            "The database could not be reached.\n\n"
            "Check your network connection, then try again.", detail)

    if isinstance(exc, SQLAlchemyError):
        _logger.debug("Unclassified SQLAlchemy error", exc_info=exc)
        return DatabaseUnavailableError(
            "Something went wrong talking to the database.\n\n"
            "Try again. If it keeps happening, contact your administrator.", detail)

    # Not a database problem at all — let the caller's own handling deal
    # with it rather than mislabelling a bug as a connection fault.
    raise exc


def user_message(exc: BaseException) -> str:
    """Headline text for a dialog or an inline error label."""
    try:
        return str(translate_db_error(exc))
    except Exception:  # noqa: BLE001 - not a database error; keep it generic
        return "Something went wrong. Please try again."
