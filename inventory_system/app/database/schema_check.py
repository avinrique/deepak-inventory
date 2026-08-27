"""Startup guard against schema drift — the database's applied migration
must match what the running code expects, checked once at process start
rather than discovered by whichever user's action first touches a column
that doesn't exist yet.

This isn't a hypothetical: a deployed database was left one migration
behind the code (missing invoices.overall_discount_amount) and the first
"look up an invoice" action crashed with a raw
sqlalchemy.exc.ProgrammingError surfaced to the user as "Looking up
invoice for sales order failed". check_schema_version() fails fast and
clearly instead, before any window is shown.

What it must *not* do is claim schema drift for a database it never
reached. The previous version caught OperationalError alongside
ProgrammingError while probing for the alembic_version table, so an
offline laptop, a typo'd host or a wrong password all produced "Database
schema has no migrations applied. Run: alembic upgrade head" — advice the
user cannot act on, for a problem they do not have. Connection failures
now propagate as their own DatabaseError (app.database.errors).
"""
import logging

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from app.core.exceptions import ResourceMissingError, SchemaVersionMismatchError
from app.core.paths import resource_path
from app.database.errors import translate_db_error
from app.database.session import get_session

_logger = logging.getLogger(__name__)


def _expected_head_revision() -> str:
    """The revision this build of the code was written against.

    Both alembic.ini and the migrations/ tree are read from disk at runtime
    — ScriptDirectory enumerates and execs the revision files — so both must
    be shipped as data by the PyInstaller spec. When they are not, that is a
    packaging defect, and ResourceMissingError says so rather than letting
    alembic's "Path doesn't exist" reach the user.
    """
    config_path = resource_path("alembic.ini")
    migrations_path = resource_path("migrations")
    if not config_path.is_file():
        raise ResourceMissingError(config_path)
    if not migrations_path.is_dir():
        raise ResourceMissingError(migrations_path)

    config = Config(str(config_path))
    config.set_main_option("script_location", str(migrations_path))
    return ScriptDirectory.from_config(config).get_current_head()


def _actual_db_revision() -> str | None:
    """The revision the database is actually at, or None if no migration has
    ever been applied to it (a brand-new, empty database).

    Only ProgrammingError is caught, and only because that is what "relation
    alembic_version does not exist" arrives as. A connection failure is a
    different problem with a different remedy and is translated and
    re-raised.
    """
    try:
        with get_session() as db:
            row = db.execute(text("SELECT version_num FROM alembic_version")).first()
            return row[0] if row is not None else None
    except ProgrammingError:
        return None
    except Exception as exc:  # noqa: BLE001 - re-raised as a translated AppError
        raise translate_db_error(exc) from exc


def database_is_empty() -> bool:
    """True when no migration has ever been applied — the signal the setup
    wizard uses to offer "Set up this database". Propagates connection
    errors, so callers must already know the database is reachable.
    """
    return _actual_db_revision() is None


def check_schema_version() -> None:
    """Call once at app startup, before any window is shown. Raises
    SchemaVersionMismatchError (a caught, user-facing failure) rather than
    letting a stale schema fail unpredictably deep inside whatever
    repository call happens to touch a missing column first, and a
    DatabaseError when the database could not be reached at all.
    """
    expected = _expected_head_revision()
    actual = _actual_db_revision()
    if actual != expected:
        _logger.error("Schema mismatch: code expects %s, database is at %s", expected, actual)
        raise SchemaVersionMismatchError(expected, actual)
    _logger.info("Database schema is at the expected revision %s", expected)
