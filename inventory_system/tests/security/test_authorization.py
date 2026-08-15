import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.security.authorization import (
    PasswordChangeRequiredError,
    PermissionDeniedError,
    check_permission,
    has_any_permission,
    has_permission,
    require_permission,
)
from app.security.session import NotAuthenticatedError, SessionExpiredError, SessionManager

T0 = datetime(2026, 1, 1, 9, 0, 0, tzinfo=timezone.utc)


def _session(manager, permissions=frozenset(), is_superuser=False,
            must_change_password=False, now=T0):
    return manager.start(user_id=uuid.uuid4(), organization_id=uuid.uuid4(),
                         role_id=uuid.uuid4(), permissions=frozenset(permissions),
                         is_superuser=is_superuser,
                         must_change_password=must_change_password, now=now)


def test_check_permission_allows_granted_code():
    manager = SessionManager(idle_timeout=timedelta(minutes=30))
    session = _session(manager, permissions={"sales.create"})
    check_permission(session, "sales.create")  # must not raise


def test_check_permission_denies_ungranted_code():
    manager = SessionManager(idle_timeout=timedelta(minutes=30))
    session = _session(manager, permissions={"sales.read"})
    with pytest.raises(PermissionDeniedError):
        check_permission(session, "sales.create")


def test_check_permission_superuser_bypasses_everything():
    manager = SessionManager(idle_timeout=timedelta(minutes=30))
    session = _session(manager, permissions=frozenset(), is_superuser=True)
    check_permission(session, "users.manage")  # must not raise


def test_check_permission_blocks_when_password_change_required():
    manager = SessionManager(idle_timeout=timedelta(minutes=30))
    session = _session(manager, permissions={"sales.create"}, must_change_password=True)
    with pytest.raises(PasswordChangeRequiredError):
        check_permission(session, "sales.create")


def test_check_permission_blocks_superuser_too_when_password_change_required():
    manager = SessionManager(idle_timeout=timedelta(minutes=30))
    session = _session(manager, is_superuser=True, must_change_password=True)
    with pytest.raises(PasswordChangeRequiredError):
        check_permission(session, "users.manage")


class _ServiceUnderTest:
    """A minimal stand-in for a real Service, using the same
    ``self._sessions`` convention require_permission relies on."""

    def __init__(self, sessions: SessionManager):
        self._sessions = sessions
        self.calls = 0

    @require_permission("sales.create")
    def protected_operation(self):
        self.calls += 1
        return "done"


def test_require_permission_allows_authorized_call():
    # require_permission checks against the real wall clock (it's the
    # actual Service-call boundary, deliberately) — start the session at
    # real "now" so it isn't already outside the idle window.
    manager = SessionManager(idle_timeout=timedelta(minutes=30))
    _session(manager, permissions={"sales.create"}, now=datetime.now(timezone.utc))
    service = _ServiceUnderTest(manager)
    assert service.protected_operation() == "done"
    assert service.calls == 1


def test_require_permission_denies_and_never_calls_the_method():
    manager = SessionManager(idle_timeout=timedelta(minutes=30))
    _session(manager, permissions={"sales.read"}, now=datetime.now(timezone.utc))
    service = _ServiceUnderTest(manager)
    with pytest.raises(PermissionDeniedError):
        service.protected_operation()
    assert service.calls == 0  # the guarded method body never ran


def test_require_permission_rejects_when_not_logged_in():
    manager = SessionManager(idle_timeout=timedelta(minutes=30))
    service = _ServiceUnderTest(manager)
    with pytest.raises(NotAuthenticatedError):
        service.protected_operation()


def test_has_permission_true_for_granted_code():
    manager = SessionManager(idle_timeout=timedelta(minutes=30))
    session = _session(manager, permissions={"sales.create"})
    assert has_permission(session, "sales.create") is True


def test_has_permission_false_for_ungranted_code():
    manager = SessionManager(idle_timeout=timedelta(minutes=30))
    session = _session(manager, permissions={"sales.read"})
    assert has_permission(session, "sales.create") is False


def test_has_permission_true_for_superuser_regardless_of_code():
    manager = SessionManager(idle_timeout=timedelta(minutes=30))
    session = _session(manager, permissions=frozenset(), is_superuser=True)
    assert has_permission(session, "users.manage_roles") is True


def test_has_permission_false_when_password_change_required_even_if_granted():
    manager = SessionManager(idle_timeout=timedelta(minutes=30))
    session = _session(manager, permissions={"sales.create"}, must_change_password=True)
    assert has_permission(session, "sales.create") is False


def test_has_permission_never_raises():
    # The whole point of has_permission over check_permission: a UI hint
    # asking "should I show this button" must get a bool back, never an
    # exception, even for a superuser mid-mandatory-password-change.
    manager = SessionManager(idle_timeout=timedelta(minutes=30))
    session = _session(manager, is_superuser=True, must_change_password=True)
    assert has_permission(session, "users.manage_roles") is False


def test_has_any_permission_true_if_any_code_is_granted():
    manager = SessionManager(idle_timeout=timedelta(minutes=30))
    session = _session(manager, permissions={"inventory.view"})
    assert has_any_permission(session, ["products.view", "inventory.view"]) is True


def test_has_any_permission_false_if_none_are_granted():
    manager = SessionManager(idle_timeout=timedelta(minutes=30))
    session = _session(manager, permissions={"sales.view"})
    assert has_any_permission(session, ["products.view", "inventory.view"]) is False


def test_has_any_permission_true_for_superuser_with_empty_codes_list_false():
    manager = SessionManager(idle_timeout=timedelta(minutes=30))
    session = _session(manager, permissions=frozenset(), is_superuser=True)
    assert has_any_permission(session, ["products.view"]) is True
    assert has_any_permission(session, []) is False  # nothing to satisfy


def test_require_permission_rejects_expired_session():
    # require_permission uses the real wall clock internally (it's the one
    # place datetime.now() is called in this module, deliberately, since
    # it's the actual Service-call boundary) — so to test expiry through it
    # we backdate last_activity_at relative to *real* now, not a fixed T0.
    manager = SessionManager(idle_timeout=timedelta(minutes=30))
    _session(manager, permissions={"sales.create"})
    manager._session.last_activity_at = (  # noqa: SLF001 - test-only introspection
        datetime.now(timezone.utc) - timedelta(hours=1))
    service = _ServiceUnderTest(manager)
    with pytest.raises(SessionExpiredError):
        service.protected_operation()
