"""Authentication: login, logout, password change. The one service allowed
to call SessionManager.start()/.end() — every other Service only reads the
current session via @require_permission.

Login/logout audit entries are written as a separate, best-effort call
after the fact (via AuditLogRepository.record, which opens its own
transaction) rather than atomically alongside another write — unlike
inventory/purchase/sales, there is no single database write here to be
atomic *with*: a session is in-memory state (SessionManager), not a row,
and record_login's own write is already a separate transaction. A rare
lost audit entry on login is an acceptable tradeoff for not restructuring
session management around it.

Two Settings values are enforced here, not hardcoded: the organization's
session_timeout_minutes is applied to the live SessionManager on every
successful login (see SessionManager.set_idle_timeout), and
change_password's new_password is checked against the organization's
password policy (app.domain.security_policy) before being accepted.
UserService.create_user enforces the same policy for admin-created
accounts — see that module.

Failed-login lockout: after _LOCKOUT_THRESHOLD failed attempts for the same
(normalized) email within _LOCKOUT_WINDOW, further attempts for that email
are rejected with AccountLockedError for _LOCKOUT_DURATION — tracked
in-memory on this instance rather than a User column, consistent with
SessionManager's own "one process, in-memory" state for this desktop app
(see security/session.py's docstring). Tracked by email regardless of
whether it belongs to a real account, so an attacker can't distinguish
"unknown email" from "real email, now locked" any faster than they could
already tell "unknown email" from "wrong password" — both already raise
the same generic InvalidCredentialsError before lockout kicks in.
"""
import uuid
from datetime import datetime, timedelta, timezone

from app.core.exceptions import (
    AccountLockedError,
    AmbiguousOrganizationError,
    InvalidCredentialsError,
    PasswordPolicyViolationError,
    UserNotFoundError,
)
from app.domain.security_policy import PasswordPolicy, validate_password
from app.domain.user import normalize_email
from app.repositories.interfaces import AuditLogRepository, OrganizationRepository, UserRepository
from app.schemas.user import MembershipOut, UserOut
from app.security.passwords import hash_password, needs_rehash, verify_password
from app.security.session import Session, SessionManager

_LOCKOUT_THRESHOLD = 5
_LOCKOUT_WINDOW = timedelta(minutes=15)
_LOCKOUT_DURATION = timedelta(minutes=15)


class AuthService:
    def __init__(self, users: UserRepository, sessions: SessionManager,
                audit_log: AuditLogRepository, organizations: OrganizationRepository):
        self._users = users
        self._sessions = sessions
        self._audit_log = audit_log
        self._organizations = organizations
        self._failed_attempts: dict[str, list[datetime]] = {}

    def _audit(self, *, action: str, user_id: uuid.UUID | None,
              organization_id: uuid.UUID | None, actor_email: str | None,
              changes: dict | None = None) -> None:
        self._audit_log.record(organization_id=organization_id, user_id=user_id,
                               actor_email=actor_email, organization_name=None,
                               action=action, entity_type="user", entity_id=user_id,
                               changes=changes)

    def _recent_failures(self, key: str, now: datetime) -> list[datetime]:
        cutoff = now - _LOCKOUT_WINDOW
        recent = [t for t in self._failed_attempts.get(key, []) if t > cutoff]
        self._failed_attempts[key] = recent
        return recent

    def _seconds_locked(self, key: str, now: datetime) -> int | None:
        recent = self._recent_failures(key, now)
        if len(recent) < _LOCKOUT_THRESHOLD:
            return None
        locked_until = recent[-1] + _LOCKOUT_DURATION
        if now >= locked_until:
            return None
        return int((locked_until - now).total_seconds())

    def _record_failure(self, key: str, now: datetime) -> None:
        self._recent_failures(key, now)
        self._failed_attempts.setdefault(key, []).append(now)

    def _clear_failures(self, key: str) -> None:
        self._failed_attempts.pop(key, None)

    def login(self, email: str, password: str,
             organization_id: uuid.UUID | None = None) -> Session:
        now = datetime.now(timezone.utc)
        lockout_key = normalize_email(email)

        seconds_locked = self._seconds_locked(lockout_key, now)
        if seconds_locked is not None:
            self._audit(action="auth.login_blocked_lockout", user_id=None,
                       organization_id=None, actor_email=email)
            raise AccountLockedError(seconds_locked)

        credentials = self._users.get_credentials_by_email(email)

        # Verify against *something* even when the email is unknown, so a
        # timing difference doesn't reveal account existence.
        if credentials is None:
            verify_password(password, hash_password("decoy-password-for-timing"))
            self._record_failure(lockout_key, now)
            self._audit(action="auth.login_failed", user_id=None, organization_id=None,
                       actor_email=email, changes={"reason": "unknown_email"})
            raise InvalidCredentialsError()
        if not credentials.is_active:
            self._record_failure(lockout_key, now)
            self._audit(action="auth.login_failed", user_id=credentials.id,
                       organization_id=None, actor_email=email,
                       changes={"reason": "account_inactive"})
            raise InvalidCredentialsError()
        if not verify_password(password, credentials.hashed_password):
            self._record_failure(lockout_key, now)
            self._audit(action="auth.login_failed", user_id=credentials.id,
                       organization_id=None, actor_email=email,
                       changes={"reason": "wrong_password"})
            raise InvalidCredentialsError()

        self._clear_failures(lockout_key)

        role_id: uuid.UUID | None = None
        org_id: uuid.UUID | None = organization_id
        permissions: frozenset[str] = frozenset()

        memberships = self._users.list_memberships(credentials.id)
        if org_id is not None:
            membership = next((m for m in memberships if m.organization_id == org_id), None)
            if membership is None and not credentials.is_superuser:
                raise InvalidCredentialsError()
        elif len(memberships) == 1:
            membership = memberships[0]
            org_id = membership.organization_id
        elif len(memberships) == 0:
            membership = None
        else:
            default = [m for m in memberships if m.is_default]
            if len(default) != 1:
                raise AmbiguousOrganizationError()
            membership = default[0]
            org_id = membership.organization_id

        if membership is not None:
            role_id = membership.role_id
            permissions = self._users.get_role_permissions(role_id)

        if org_id is not None:
            org = self._organizations.get_by_id(org_id)
            if org is not None:
                self._sessions.set_idle_timeout(
                    timedelta(minutes=org.session_timeout_minutes))

        if org_id is not None and needs_rehash(credentials.hashed_password):
            self._users.update_password_hash(
                credentials.id, org_id, hash_password(password),
                must_change_password=credentials.must_change_password)

        session = self._sessions.start(
            user_id=credentials.id, organization_id=org_id, role_id=role_id,
            permissions=permissions, is_superuser=credentials.is_superuser,
            must_change_password=credentials.must_change_password, now=now)
        self._users.record_login(credentials.id, now)
        self._audit(action="auth.login_succeeded", user_id=credentials.id,
                   organization_id=org_id, actor_email=email)
        return session

    def logout(self) -> None:
        # Captured before .end() clears it — nothing to attribute the audit
        # entry to afterward.
        session = self._sessions.peek()
        self._sessions.end()
        if session is not None:
            self._audit(action="auth.logout", user_id=session.user_id,
                       organization_id=session.organization_id, actor_email=None)

    def change_password(self, old_password: str, new_password: str) -> None:
        session = self._sessions.current(now=datetime.now(timezone.utc))
        credentials = self._users.get_credentials_by_id(session.user_id)
        if credentials is None or not verify_password(old_password,
                                                       credentials.hashed_password):
            raise InvalidCredentialsError()

        if session.organization_id is not None:
            org = self._organizations.get_by_id(session.organization_id)
            if org is not None:
                policy = PasswordPolicy(
                    min_length=org.password_min_length,
                    require_uppercase=org.password_require_uppercase,
                    require_number=org.password_require_number,
                    require_special_char=org.password_require_special_char)
                errors = validate_password(new_password, policy)
                if errors:
                    raise PasswordPolicyViolationError(errors)

        self._users.update_password_hash(session.user_id, session.organization_id,
                                         hash_password(new_password),
                                         must_change_password=False)
        self._sessions.mark_password_changed()
        # Records that a change happened, never what the password is (or
        # was) — the audit entry carries no hash, no plaintext, nothing
        # password-shaped at all, same as reset_password's audit entry.
        self._audit(action="auth.password_changed", user_id=session.user_id,
                   organization_id=session.organization_id, actor_email=None)

    def get_current_user(self) -> UserOut:
        """The full profile of whoever is logged in — the one place the UI
        should reach for "current user" (full name, email, ...) instead of
        going through a repository directly, so that stays consistent
        everywhere it's shown (header, status bar, self-action guards) and
        goes through the same session-expiry check as every other read.
        """
        session = self._sessions.current(now=datetime.now(timezone.utc))
        user = self._users.get_by_id(session.user_id)
        if user is None:
            raise UserNotFoundError(session.user_id)
        return user

    def get_current_membership(self) -> MembershipOut | None:
        """None for a superuser acting with no organization membership, or
        any session with no organization_id — same "no membership" shape
        MainWindow already handled ad hoc before this existed.
        """
        session = self._sessions.current(now=datetime.now(timezone.utc))
        if session.organization_id is None:
            return None
        return self._users.get_membership(session.user_id, session.organization_id)

    def is_current_user_still_active(self) -> bool:
        """Whether the logged-in user's account is still active *right
        now*, per the database — not from the session's own state, which
        never updates itself. A permission check (require_permission) only
        looks at the session's cached snapshot from login time, so it has
        no way to notice a deactivation that happened after login; this is
        what a periodic UI poll (see MainWindow's idle-check timer) uses to
        actually enforce "a deactivated user's live session stops working",
        not just "a deactivated user can't log in again". Returns True for
        "nothing to check" (no session, or the user row is gone some other
        way) — this method only ever *ends* a session, it doesn't start
        raising where nothing was wrong before — but a user row that's
        vanished entirely (deleted, not just deactivated) is treated the
        same as "deactivated": there's no active account to keep a session
        open for either way.
        """
        session = self._sessions.peek()
        if session is None:
            return True
        credentials = self._users.get_credentials_by_id(session.user_id)
        return credentials is not None and credentials.is_active
