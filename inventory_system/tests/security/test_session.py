import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.security.session import (
    NotAuthenticatedError,
    SessionExpiredError,
    SessionManager,
)

T0 = datetime(2026, 1, 1, 9, 0, 0, tzinfo=timezone.utc)
USER_ID = uuid.uuid4()
ORG_ID = uuid.uuid4()
ROLE_ID = uuid.uuid4()


def _manager(minutes=30):
    return SessionManager(idle_timeout=timedelta(minutes=minutes))


def _start(manager, now=T0, **overrides):
    kwargs = dict(user_id=USER_ID, organization_id=ORG_ID, role_id=ROLE_ID,
                 permissions=frozenset({"sales.read"}), is_superuser=False,
                 must_change_password=False, now=now)
    kwargs.update(overrides)
    return manager.start(**kwargs)


def test_current_raises_before_any_login():
    manager = _manager()
    with pytest.raises(NotAuthenticatedError):
        manager.current(now=T0)


def test_current_returns_session_right_after_start():
    manager = _manager()
    started = _start(manager)
    fetched = manager.current(now=T0)
    assert fetched.id == started.id
    assert fetched.user_id == USER_ID


def test_current_still_valid_just_under_the_timeout():
    manager = _manager(minutes=30)
    _start(manager)
    just_under = T0 + timedelta(minutes=29, seconds=59)
    manager.current(now=just_under)  # must not raise


def test_current_expires_after_idle_timeout():
    manager = _manager(minutes=30)
    _start(manager)
    later = T0 + timedelta(minutes=31)
    with pytest.raises(SessionExpiredError):
        manager.current(now=later)


def test_expired_session_cannot_be_resumed():
    manager = _manager(minutes=30)
    _start(manager)
    with pytest.raises(SessionExpiredError):
        manager.current(now=T0 + timedelta(minutes=31))
    # a second check afterwards is "not authenticated", not "expired" again —
    # the expired session was cleared, not left dangling
    with pytest.raises(NotAuthenticatedError):
        manager.current(now=T0 + timedelta(minutes=32))


def test_activity_resets_the_idle_clock():
    manager = _manager(minutes=30)
    _start(manager)
    manager.current(now=T0 + timedelta(minutes=20))  # activity extends it
    manager.current(now=T0 + timedelta(minutes=45))  # would've expired from T0, not from +20


def test_logout_ends_the_session():
    manager = _manager()
    _start(manager)
    manager.end()
    with pytest.raises(NotAuthenticatedError):
        manager.current(now=T0)


def test_touch_extends_an_active_session():
    manager = _manager(minutes=30)
    _start(manager)
    manager.touch(T0 + timedelta(minutes=20))
    # would have expired if last_activity_at were still T0
    manager.current(now=T0 + timedelta(minutes=45))


def test_touch_is_a_noop_when_logged_out():
    manager = _manager()
    manager.touch(T0)  # must not raise


def test_touch_does_not_resurrect_an_already_expired_session():
    manager = _manager(minutes=30)
    _start(manager)
    far_future = T0 + timedelta(hours=5)
    manager.touch(far_future)  # session is already idle-expired by now
    with pytest.raises(SessionExpiredError):
        manager.current(now=far_future)


def test_is_idle_expired_false_when_not_logged_in():
    manager = _manager()
    assert manager.is_idle_expired(T0) is False


def test_is_idle_expired_false_within_window():
    manager = _manager(minutes=30)
    _start(manager)
    assert manager.is_idle_expired(T0 + timedelta(minutes=10)) is False


def test_is_idle_expired_true_past_window():
    manager = _manager(minutes=30)
    _start(manager)
    assert manager.is_idle_expired(T0 + timedelta(minutes=31)) is True


def test_is_idle_expired_does_not_mutate_state():
    manager = _manager(minutes=30)
    _start(manager)
    manager.is_idle_expired(T0 + timedelta(minutes=31))
    # peek still sees the (stale but not cleared) session
    assert manager.peek() is not None


def test_is_authenticated_reflects_state():
    manager = _manager()
    assert manager.is_authenticated is False
    _start(manager)
    assert manager.is_authenticated is True
    manager.end()
    assert manager.is_authenticated is False


def test_peek_does_not_extend_or_validate():
    manager = _manager(minutes=30)
    _start(manager)
    # peek after the timeout should still return the (stale) session object
    # rather than raising — it's a non-authoritative look, not a check
    stale = manager.peek()
    assert stale is not None
    assert stale.user_id == USER_ID


def test_mark_password_changed_clears_the_flag():
    manager = _manager()
    _start(manager, must_change_password=True)
    assert manager.current(now=T0).must_change_password is True
    manager.mark_password_changed()
    assert manager.current(now=T0).must_change_password is False


def test_mark_password_changed_is_a_noop_when_logged_out():
    manager = _manager()
    manager.mark_password_changed()  # must not raise


def test_set_idle_timeout_changes_the_effective_timeout_live():
    manager = _manager(minutes=30)
    _start(manager)
    manager.set_idle_timeout(timedelta(minutes=5))

    # 10 minutes idle: fine under the original 30-minute timeout, expired
    # under the new 5-minute one — proves the change took effect on the
    # already-running session, not just future ones.
    later = T0 + timedelta(minutes=10)
    with pytest.raises(SessionExpiredError):
        manager.current(now=later)


def test_set_idle_timeout_can_also_extend_it():
    manager = _manager(minutes=5)
    _start(manager)
    manager.set_idle_timeout(timedelta(minutes=30))

    later = T0 + timedelta(minutes=10)
    assert manager.current(now=later).user_id == USER_ID
