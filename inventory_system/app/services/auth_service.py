"""Authentication: login, logout, password change. The one service allowed
to call SessionManager.start()/.end() — every other Service only reads the
current session via @require_permission.
"""
import uuid
from datetime import datetime, timezone

from app.core.exceptions import AmbiguousOrganizationError, InvalidCredentialsError
from app.repositories.interfaces import UserRepository
from app.security.passwords import hash_password, needs_rehash, verify_password
from app.security.session import Session, SessionManager


class AuthService:
    def __init__(self, users: UserRepository, sessions: SessionManager):
        self._users = users
        self._sessions = sessions

    def login(self, email: str, password: str,
             organization_id: uuid.UUID | None = None) -> Session:
        now = datetime.now(timezone.utc)
        credentials = self._users.get_credentials_by_email(email)

        # Verify against *something* even when the email is unknown, so a
        # timing difference doesn't reveal account existence.
        if credentials is None:
            verify_password(password, hash_password("decoy-password-for-timing"))
            raise InvalidCredentialsError()
        if not credentials.is_active:
            raise InvalidCredentialsError()
        if not verify_password(password, credentials.hashed_password):
            raise InvalidCredentialsError()

        if needs_rehash(credentials.hashed_password):
            self._users.update_password_hash(
                credentials.id, hash_password(password),
                must_change_password=credentials.must_change_password)

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

        session = self._sessions.start(
            user_id=credentials.id, organization_id=org_id, role_id=role_id,
            permissions=permissions, is_superuser=credentials.is_superuser,
            must_change_password=credentials.must_change_password, now=now)
        self._users.record_login(credentials.id, now)
        return session

    def logout(self) -> None:
        self._sessions.end()

    def change_password(self, old_password: str, new_password: str) -> None:
        session = self._sessions.current(now=datetime.now(timezone.utc))
        credentials = self._users.get_credentials_by_id(session.user_id)
        if credentials is None or not verify_password(old_password,
                                                       credentials.hashed_password):
            raise InvalidCredentialsError()
        self._users.update_password_hash(session.user_id, hash_password(new_password),
                                         must_change_password=False)
        self._sessions.mark_password_changed()
