"""UI-layer permission hints — the one place a page or the sidebar asks
"should I show/enable this?" Delegates entirely to
app.security.authorization (has_permission/has_any_permission), the same
code every Service's @require_permission uses, so a UI hint here can never
drift out of sync with what the server actually allows and no page
reimplements the superuser-bypass / must_change_password rules on its own
(four pages — Products, Users, Inventory, Settings — each used to do a
slightly different ad hoc "code in session.permissions" check before this
existed; none of them handled superuser correctly).

This is a UX convenience ONLY: hiding a button here does not, by itself,
stop anyone from calling the underlying Service method directly (a test, a
script, a future second UI front-end). That boundary is @require_permission
on the Service method, enforced independently every time — see
app.security.authorization's module docstring.
"""
from collections.abc import Iterable
from datetime import datetime, timezone

from app.security.authorization import has_any_permission, has_permission
from app.security.session import Session, SessionManager


def _current_session(sessions: SessionManager) -> Session | None:
    # Deliberately .peek() rather than .current(): a UI hint must never
    # raise (NotAuthenticatedError/SessionExpiredError) or have the side
    # effect of touching/clearing the session the way a real protected call
    # does — it's just a question about what to render.
    if sessions.is_idle_expired(datetime.now(timezone.utc)):
        return None
    return sessions.peek()


def can(sessions: SessionManager, code: str) -> bool:
    """Whether the current session can, right now, do ``code`` — same
    rules @require_permission enforces (superuser bypass,
    must_change_password block), just non-raising. Use to show/hide or
    enable/disable a single action (a button, a menu item).
    """
    session = _current_session(sessions)
    return session is not None and has_permission(session, code)


def can_any(sessions: SessionManager, codes: Iterable[str]) -> bool:
    """Whether the current session can do at least one of ``codes`` — for
    gating something satisfied by any of several alternative permissions
    (e.g. a sidebar entry covering a module with more than one relevant
    permission code).
    """
    session = _current_session(sessions)
    return session is not None and has_any_permission(session, codes)
