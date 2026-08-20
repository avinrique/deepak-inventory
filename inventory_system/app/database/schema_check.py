"""Startup guard against schema drift — the database's applied migration
must match what the running code expects, checked once at process start
rather than discovered by whichever user's action first touches a column
that doesn't exist yet.

This isn't a hypothetical: a deployed database was left one migration
behind the code (missing invoices.overall_discount_amount) and the first
"look up an invoice" action crashed with a raw
sqlalchemy.exc.ProgrammingError surfaced to the user as "Looking up
invoice for sales order failed" — see logs/inventory_system.log and
SchemaVersionMismatchError's docstring. check_schema_version() fails fast
and clearly instead, before any window is shown.
"""
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import OperationalError, ProgrammingError

from app.core.exceptions import SchemaVersionMismatchError
from app.database.session import get_session

# inventory_system/app/database/schema_check.py -> inventory_system/
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _expected_head_revision() -> str:
    config = Config(str(_PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_PROJECT_ROOT / "migrations"))
    script = ScriptDirectory.from_config(config)
    return script.get_current_head()


def _actual_db_revision() -> str | None:
    try:
        with get_session() as db:
            row = db.execute(text("SELECT version_num FROM alembic_version")).first()
            return row[0] if row is not None else None
    except (OperationalError, ProgrammingError):
        # alembic_version doesn't exist yet -> no migration has ever been
        # applied to this database, same as "actual is None" below.
        return None


def check_schema_version() -> None:
    """Call once at app startup, before any window is shown. Raises
    SchemaVersionMismatchError (a caught, user-facing failure) rather than
    letting a stale schema fail unpredictably deep inside whatever
    repository call happens to touch a missing column first.
    """
    expected = _expected_head_revision()
    actual = _actual_db_revision()
    if actual != expected:
        raise SchemaVersionMismatchError(expected, actual)
