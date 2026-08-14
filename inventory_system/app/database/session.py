"""SQLAlchemy engine/session plumbing. Phase 2.

Engine creation is deliberately lazy (inside _init(), not at import time) so
`import app.database.session` never requires a live PostgreSQL / psycopg to
be installed — only calling get_session() does. Safe to import today even
though no database is configured in this environment.
"""
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config.settings import settings

_engine = None
_SessionLocal = None


def _init() -> None:
    global _engine, _SessionLocal
    if _engine is None:
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
                                pool_pre_ping=True, pool_recycle=1800)
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, future=True)


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
