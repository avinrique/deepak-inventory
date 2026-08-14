"""User administration: create, activate/deactivate, admin-initiated
password reset. Every method here is a protected operation — see each
@require_permission("users.manage").
"""
import secrets
import uuid

from app.repositories.interfaces import UserRepository
from app.schemas.user import UserOut
from app.security.authorization import require_permission
from app.security.passwords import hash_password
from app.security.session import SessionManager


class UserService:
    def __init__(self, users: UserRepository, sessions: SessionManager):
        self._users = users
        self._sessions = sessions

    @require_permission("users.manage")
    def create_user(self, email: str, full_name: str, initial_password: str,
                    organization_id: uuid.UUID, role_id: uuid.UUID) -> UserOut:
        return self._users.create_user(email=email, full_name=full_name,
                                       hashed_password=hash_password(initial_password),
                                       organization_id=organization_id, role_id=role_id)

    @require_permission("users.manage")
    def activate_user(self, target_user_id: uuid.UUID) -> None:
        self._users.set_active(target_user_id, True)

    @require_permission("users.manage")
    def deactivate_user(self, target_user_id: uuid.UUID) -> None:
        self._users.set_active(target_user_id, False)

    @require_permission("users.manage")
    def reset_password(self, target_user_id: uuid.UUID) -> str:
        """Admin-initiated reset: there is no email/SMS infrastructure in
        this desktop app, so the realistic mechanism is a random temporary
        password relayed out-of-band by whoever has users.manage — not an
        emailed reset link. Returned once; the target user must change it
        (must_change_password=True) before doing anything else — enforced
        in app.security.authorization, not left to the UI to remember.
        """
        temporary_password = secrets.token_urlsafe(12)
        self._users.update_password_hash(target_user_id, hash_password(temporary_password),
                                         must_change_password=True)
        return temporary_password
