"""In-process session for this desktop app.

One user is "logged in" to the running GUI at a time — e.g. a shared
terminal at a shop counter where staff log in/out over the course of a day
— tracked by *idle* time rather than a fixed expiry: walking away should
eventually log you out, typing continuously past some fixed deadline
shouldn't. SessionManager never calls datetime.now() itself; callers pass
``now`` explicitly, which is what makes it fully deterministic to test
(see tests/security/test_session.py) without sleeping or monkeypatching.
"""
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta


class NotAuthenticatedError(Exception):
    """No active session — the caller must log in first."""


class SessionExpiredError(Exception):
    """The session existed but has been idle longer than the timeout."""


@dataclass
class Session:
    id: uuid.UUID
    user_id: uuid.UUID
    organization_id: uuid.UUID | None
    role_id: uuid.UUID | None
    permissions: frozenset[str]
    is_superuser: bool
    must_change_password: bool
    created_at: datetime
    last_activity_at: datetime

    def is_expired(self, now: datetime, idle_timeout: timedelta) -> bool:
        return now - self.last_activity_at > idle_timeout


class SessionManager:
    """Holds the single active session for this process."""

    def __init__(self, idle_timeout: timedelta):
        self._idle_timeout = idle_timeout
        self._session: Session | None = None

    def set_idle_timeout(self, idle_timeout: timedelta) -> None:
        """Lets the configured timeout change live, without restarting the
        app — called with the organization's session_timeout_minutes
        Settings value on login (AuthService.login) and whenever that
        setting is saved (OrganizationService.update_organization), so a
        change takes effect for the current session immediately rather
        than only on the next login.
        """
        self._idle_timeout = idle_timeout

    def start(self, *, user_id: uuid.UUID, organization_id: uuid.UUID | None,
             role_id: uuid.UUID | None, permissions: frozenset[str], is_superuser: bool,
             must_change_password: bool, now: datetime) -> Session:
        self._session = Session(id=uuid.uuid4(), user_id=user_id,
                                organization_id=organization_id, role_id=role_id,
                                permissions=frozenset(permissions),
                                is_superuser=is_superuser,
                                must_change_password=must_change_password,
                                created_at=now, last_activity_at=now)
        return self._session

    def end(self) -> None:
        self._session = None

    def peek(self) -> Session | None:
        """The current session without touching or expiry-checking it —
        for callers (like logout) that need to know who *was* logged in
        without extending or validating the session."""
        return self._session

    def current(self, *, now: datetime) -> Session:
        """The active session, raising if there isn't one or it's expired.
        Touches (extends) it on success — every check is itself activity.
        """
        session = self._session
        if session is None:
            raise NotAuthenticatedError("No active session — log in first.")
        if session.is_expired(now, self._idle_timeout):
            self._session = None
            raise SessionExpiredError(
                "Session timed out due to inactivity — log in again.")
        session.last_activity_at = now
        return session

    def mark_password_changed(self) -> None:
        if self._session is not None:
            self._session.must_change_password = False

    def touch(self, now: datetime) -> None:
        """Passive activity ping — e.g. from a UI event filter on mouse/key
        events. Unlike current(), never raises and never clears an expired
        session; it's a no-op if there's nothing to extend. The next
        current() call is what actually enforces expiry.
        """
        if self._session is not None and not self._session.is_expired(
                now, self._idle_timeout):
            self._session.last_activity_at = now

    def is_idle_expired(self, now: datetime) -> bool:
        """Non-destructive expiry check for a UI poll loop deciding whether
        to force a return to the login screen — unlike current(), doesn't
        raise and doesn't clear the session (the caller does that, via
        end(), once it's actually acted on the expiry)."""
        return self._session is not None and self._session.is_expired(
            now, self._idle_timeout)

    @property
    def is_authenticated(self) -> bool:
        return self._session is not None
