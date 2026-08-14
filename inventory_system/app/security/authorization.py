"""Authorization enforcement.

This is the boundary that actually matters: the UI hides buttons a user
can't use as a convenience, but every Service method that performs a
protected operation must go through @require_permission (or call
check_permission directly) — never trust that a call only arrives here
because the UI allowed it. A Service method calling storage/a repository
directly without this check is a bug, not a shortcut.

@require_permission expects the decorated method's class to expose a
``self._sessions: SessionManager`` attribute (the same convention every
Service in this codebase uses) — it looks up the current session there,
so call sites stay plain (``user_service.deactivate_user(id)``) rather than
threading a session through every call.
"""
from datetime import datetime, timezone
from functools import wraps

from app.security.session import Session, SessionManager


class PermissionDeniedError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(f"Missing permission: {code!r}")


class PasswordChangeRequiredError(Exception):
    """Raised instead of PermissionDeniedError when the session's user must
    change their password (e.g. after an admin-initiated reset) before any
    other protected operation is allowed.
    """


def check_permission(session: Session, code: str) -> None:
    if session.must_change_password:
        raise PasswordChangeRequiredError(
            "Password must be changed before continuing.")
    if session.is_superuser:
        return
    if code not in session.permissions:
        raise PermissionDeniedError(code)


def require_permission(code: str):
    def decorator(fn):
        @wraps(fn)
        def wrapper(self, *args, **kwargs):
            sessions: SessionManager = self._sessions
            session = sessions.current(now=datetime.now(timezone.utc))
            check_permission(session, code)
            return fn(self, *args, **kwargs)
        return wrapper
    return decorator
