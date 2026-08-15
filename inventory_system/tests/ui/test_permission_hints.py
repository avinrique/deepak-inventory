"""app.ui.permission_hints against a real SessionManager — no PySide6
involved, since these are plain functions over SessionManager/Session.
Proves the UI-hint layer delegates to app.security.authorization (same
superuser/must_change_password rules) rather than reimplementing them, and
never raises even when there's nothing to check against.
"""
import uuid
from datetime import datetime, timedelta, timezone

from app.security.session import SessionManager
from app.ui import permission_hints


def _session(manager, permissions=frozenset(), is_superuser=False,
            must_change_password=False, now=None):
    # permission_hints.can()/can_any() check idle-expiry against the real
    # wall clock (via SessionManager.is_idle_expired), unlike
    # check_permission — so, unlike test_authorization.py's T0-seeded
    # sessions, these must be seeded at real "now" or every session here
    # would already read as idle-expired.
    now = now or datetime.now(timezone.utc)
    return manager.start(user_id=uuid.uuid4(), organization_id=uuid.uuid4(),
                         role_id=uuid.uuid4(), permissions=frozenset(permissions),
                         is_superuser=is_superuser,
                         must_change_password=must_change_password, now=now)


def test_can_true_for_granted_code():
    manager = SessionManager(idle_timeout=timedelta(minutes=30))
    _session(manager, permissions={"products.view"})
    assert permission_hints.can(manager, "products.view") is True


def test_can_false_for_ungranted_code():
    manager = SessionManager(idle_timeout=timedelta(minutes=30))
    _session(manager, permissions={"products.view"})
    assert permission_hints.can(manager, "products.create") is False


def test_can_true_for_superuser_regardless_of_granted_codes():
    manager = SessionManager(idle_timeout=timedelta(minutes=30))
    _session(manager, permissions=frozenset(), is_superuser=True)
    assert permission_hints.can(manager, "users.manage_roles") is True


def test_can_false_when_nobody_is_logged_in():
    manager = SessionManager(idle_timeout=timedelta(minutes=30))
    assert permission_hints.can(manager, "products.view") is False


def test_can_false_once_idle_expired_without_raising():
    manager = SessionManager(idle_timeout=timedelta(minutes=30))
    _session(manager, permissions={"products.view"})
    manager._session.last_activity_at -= timedelta(hours=1)  # noqa: SLF001
    # Must not raise SessionExpiredError the way .current() would — a
    # button's visibility check has no business ending the session.
    assert permission_hints.can(manager, "products.view") is False
    assert manager.is_authenticated is True  # peek()-based: never cleared as a side effect


def test_can_false_while_password_change_is_mandatory():
    manager = SessionManager(idle_timeout=timedelta(minutes=30))
    _session(manager, permissions={"products.view"}, must_change_password=True)
    assert permission_hints.can(manager, "products.view") is False


def test_can_any_true_if_one_of_several_codes_is_granted():
    manager = SessionManager(idle_timeout=timedelta(minutes=30))
    _session(manager, permissions={"inventory.view"})
    assert permission_hints.can_any(manager, ["products.view", "inventory.view"]) is True


def test_can_any_false_if_none_are_granted():
    manager = SessionManager(idle_timeout=timedelta(minutes=30))
    _session(manager, permissions={"sales.view"})
    assert permission_hints.can_any(manager, ["products.view", "inventory.view"]) is False


def test_can_any_true_for_superuser_even_with_empty_permissions():
    manager = SessionManager(idle_timeout=timedelta(minutes=30))
    _session(manager, permissions=frozenset(), is_superuser=True)
    assert permission_hints.can_any(manager, ["users.view"]) is True


def test_can_any_false_when_nobody_is_logged_in():
    manager = SessionManager(idle_timeout=timedelta(minutes=30))
    assert permission_hints.can_any(manager, ["products.view", "inventory.view"]) is False
