"""Regression test: every sidebar module must actually construct via
app.ui.main_window._build_page for a fully-permissioned session.

This class of bug happened for real during this codebase's development: a
page constructor's signature changed (WarehousesPage/SuppliersPage started
requiring (service, sessions)) while _build_page still called it with no
arguments — a TypeError that would have fired for every user immediately
after login, since MainWindow.__init__ eagerly builds every visible
module's page before the window is even shown. Nothing in the test suite
caught it before it reached the running app.

Uses MagicMock Container/services (no real Container(), no real database)
so this stays a pure widget-construction smoke test — page __init__
methods don't perform I/O themselves (data loading is deferred to
AsyncContentArea/Worker), but a real Container would still wire real
SqlXRepository instances against settings.database_url, and this test
must never risk touching that real database.
"""
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

try:
    from PySide6.QtWidgets import QApplication, QWidget
except ImportError:
    pytest.skip("PySide6 not available", allow_module_level=True)

from app.security.session import SessionManager
from app.ui.main_window import MODULES


@pytest.fixture(scope="module")
def qapp():
    try:
        return QApplication.instance() or QApplication([])
    except Exception as exc:  # noqa: BLE001 - e.g. no display available
        pytest.skip(f"cannot create QApplication: {exc}")


@pytest.fixture()
def fully_permissioned_sessions():
    sessions = SessionManager(idle_timeout=timedelta(minutes=30))
    sessions.start(user_id=uuid.uuid4(), organization_id=uuid.uuid4(), role_id=uuid.uuid4(),
                   permissions=frozenset(), is_superuser=True,
                   must_change_password=False, now=datetime.now(timezone.utc))
    return sessions


@pytest.fixture()
def fake_container(fully_permissioned_sessions):
    container = MagicMock()
    container.sessions = fully_permissioned_sessions
    # Every container.<x>_service() call returns a fresh MagicMock — page
    # constructors only store these, they don't call methods on them until
    # AsyncContentArea/a button click triggers a Worker, which this
    # synchronous construction-only test never does.
    return container


@pytest.mark.parametrize("module", MODULES, ids=lambda m: m.key)
def test_build_page_constructs_every_module(qapp, fake_container, module):
    from app.ui.main_window import _build_page

    widget = _build_page(module.key, fake_container)
    assert isinstance(widget, QWidget)
