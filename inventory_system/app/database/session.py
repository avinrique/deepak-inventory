"""SQLAlchemy engine/session plumbing.

Engine creation is deliberately lazy (inside _init(), not at import time) so
`import app.database.session` never requires a reachable PostgreSQL — only
calling get_session() does. That is what lets the app start, show a window,
and run the setup wizard on a machine that has no database configured yet.
"""
import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.config.settings import settings
from app.database.errors import translate_db_error

_logger = logging.getLogger(__name__)

_engine = None
_SessionLocal = None


def _connect_args() -> dict:
    """psycopg's connect_timeout, in seconds.

    Without it an unreachable host blocks on the OS-level TCP timeout —
    around two minutes on Windows — during which the app is frozen on a
    splash screen with no way to cancel and no message. A bounded wait lets
    startup fail into a real "could not reach the database" dialog.
    """
    return {"connect_timeout": max(1, settings.db_connect_timeout)}


def _init() -> None:
    global _engine, _SessionLocal
    if _engine is None:
        if not settings.database_url:
            # Reaching here means something skipped the setup wizard; a
            # clear error beats create_engine's "Could not parse URL".
            raise translate_db_error(SQLAlchemyError(
                "No database has been configured yet."))
        # pool_pre_ping: a desktop app can sit idle for hours — without
        # this, a connection the Postgres server (or an intervening
        # firewall/NAT) has silently dropped gets handed back out of the
        # pool and fails on first use with an opaque OperationalError.
        # pre_ping runs a cheap "SELECT 1" before reuse and transparently
        # reconnects if that fails, rather than surfacing the failure to a
        # caller mid-transaction. pool_recycle proactively retires
        # connections older than 30 minutes for the same reason, before
        # they have a chance to go stale.
        _engine = create_engine(settings.database_url, future=True,
                                pool_pre_ping=True, pool_recycle=1800,
                                connect_args=_connect_args())
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, future=True)


def reset_engine() -> None:
    """Drops the engine so the next get_session() rebuilds it from current
    settings. Called after the setup wizard saves a new connection —
    otherwise the process would keep using the pool it built from the old
    URL for the rest of its life.
    """
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None


def test_connection(database_url: str, timeout_seconds: int | None = None) -> None:
    """Opens a throwaway connection to `database_url`, then closes it.

    Used by the setup wizard to validate what the admin typed *before*
    saving it, and by startup to tell "cannot reach the database" apart from
    "schema out of date". Deliberately builds its own engine rather than
    touching the module-global one: the URL being tested is not (yet) the
    configured URL, and a failed probe must not poison the app's pool.

    Raises the translated AppError, never a driver exception.
    """
    timeout = timeout_seconds if timeout_seconds is not None else settings.db_connect_timeout
    engine = create_engine(database_url, future=True, poolclass=None,
                           connect_args={"connect_timeout": max(1, timeout)})
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - re-raised as a translated AppError
        raise translate_db_error(exc) from exc
    finally:
        engine.dispose()


@contextmanager
def get_session() -> Iterator[Session]:
    _init()
    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
