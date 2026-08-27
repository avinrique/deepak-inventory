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

try:
    from PySide6.QtCore import QEvent, QThreadPool
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover - drain_qt_work becomes a no-op
    QApplication = None  # type: ignore[assignment]

# Generous, but only ever waited out when a test really did leave a Worker
# running; an idle pool returns immediately.
_DRAIN_TIMEOUT_MS = 5000
_DRAIN_PASSES = 3


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


@pytest.fixture(autouse=True)
def drain_qt_work():
    """Finishes and tears down whatever Qt work a test leaves behind.

    UI tests build widgets and let them fall out of scope without ever
    running an event loop, so three kinds of work outlive them:

    * ``Worker``s already handed to ``QThreadPool.globalInstance()``, each
      holding a widget's bound method as its ``finished`` slot;
    * the ``QTimer.singleShot(0, ...)`` that ``Worker._release`` posts (see
      app/workers/base_worker.py);
    * ``DeferredDelete`` events Qt itself posts for cell widgets whenever a
      table is cleared — ``TransactionItemsTable.clear_items`` calls
      ``setRowCount(0)`` and then drops its own references to those widgets.

    Nothing drains that queue until some *later* test starts a real loop
    (tests/workers/test_worker_multi_slot_delivery.py calls ``qapp.exec()``),
    which then delivers the whole backlog to objects CPython has since
    freed. That segfaults, and the blame lands on whichever test happened to
    start the loop rather than the one that queued the work.

    So: wait out the running workers, flush the posted callbacks, then close
    and delete the test's top-level widgets in an order Qt controls instead
    of leaving it to whenever the wrappers get collected. Ordering matters —
    flushing ``DeferredDelete`` *before* closing the windows destroys the C++
    half of widgets the finished test still has wrappers for.
    """
    yield
    if QApplication is None:
        return
    app = QApplication.instance()
    if app is None:
        return
    QThreadPool.globalInstance().waitForDone(_DRAIN_TIMEOUT_MS)
    # Repeated because delivery cascades: a Worker's finished signal runs a
    # slot that posts the QTimer.singleShot in Worker._release, and a page's
    # render callback can start another Worker. One pass leaves the tail of
    # that chain queued.
    for _ in range(_DRAIN_PASSES):
        app.processEvents()
        QThreadPool.globalInstance().waitForDone(_DRAIN_TIMEOUT_MS)

    # Only now tear the widgets down, in an order Qt controls rather than
    # whenever CPython happens to free the wrappers. Deleting before the
    # queue is drained delivers the remainder to freed objects, which is the
    # crash this fixture exists to prevent.
    for widget in app.topLevelWidgets():
        widget.close()
        widget.deleteLater()
    app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
