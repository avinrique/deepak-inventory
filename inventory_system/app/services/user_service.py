"""User administration: create, activate/deactivate, admin-initiated
password reset, role/permission assignment. Every method here is a
protected operation — see each @require_permission("users.manage").

Audit entries are written as a separate, best-effort call after the
underlying write succeeds (see app.services.auth_service's module
docstring for why that's an acceptable tradeoff here, same reasoning).

create_user's initial_password is checked against the target
organization's password policy (app.domain.security_policy) before being
accepted — the same policy AuthService.change_password enforces for
self-service changes. reset_password's system-generated temporary
password is exempt: secrets.token_urlsafe output is high-entropy by
construction, and the target user is forced to replace it on next login
anyway (must_change_password=True), at which point change_password's
policy check applies to whatever they pick.
"""
import secrets
import uuid

from app.core.exceptions import MembershipNotFoundError, PasswordPolicyViolationError
from app.domain.security_policy import PasswordPolicy, validate_password
from app.repositories.interfaces import AuditLogRepository, OrganizationRepository, UserRepository
from app.schemas.user import MembershipOut, UserOut
from app.security.authorization import require_permission
from app.security.passwords import hash_password
from app.security.session import SessionManager


class UserService:
    def __init__(self, users: UserRepository, sessions: SessionManager,
                audit_log: AuditLogRepository, organizations: OrganizationRepository):
        self._users = users
        self._sessions = sessions
        self._audit_log = audit_log
        self._organizations = organizations

    def _actor(self) -> tuple[uuid.UUID | None, uuid.UUID | None]:
        session = self._sessions.peek()
        if session is None:
            return None, None
        return session.user_id, session.organization_id

    def _audit(self, *, action: str, entity_id: uuid.UUID, changes: dict | None = None) -> None:
        actor_id, org_id = self._actor()
        self._audit_log.record(organization_id=org_id, user_id=actor_id, actor_email=None,
                               organization_name=None, action=action, entity_type="user",
                               entity_id=entity_id, changes=changes)

    @require_permission("users.manage")
    def create_user(self, email: str, full_name: str, initial_password: str,
                    organization_id: uuid.UUID, role_id: uuid.UUID) -> UserOut:
        org = self._organizations.get_by_id(organization_id)
        if org is not None:
            policy = PasswordPolicy(
                min_length=org.password_min_length,
                require_uppercase=org.password_require_uppercase,
                require_number=org.password_require_number,
                require_special_char=org.password_require_special_char)
            errors = validate_password(initial_password, policy)
            if errors:
                raise PasswordPolicyViolationError(errors)

        created = self._users.create_user(email=email, full_name=full_name,
                                          hashed_password=hash_password(initial_password),
                                          organization_id=organization_id, role_id=role_id)
        self._audit(action="user.create", entity_id=created.id,
                   changes={"email": created.email, "full_name": created.full_name,
                           "organization_id": str(organization_id), "role_id": str(role_id)})
        return created

    @require_permission("users.manage")
    def activate_user(self, target_user_id: uuid.UUID) -> None:
        self._users.set_active(target_user_id, True)
        self._audit(action="user.activate", entity_id=target_user_id)

    @require_permission("users.manage")
    def deactivate_user(self, target_user_id: uuid.UUID) -> None:
        self._users.set_active(target_user_id, False)
        self._audit(action="user.deactivate", entity_id=target_user_id)

    @require_permission("users.manage")
    def reset_password(self, target_user_id: uuid.UUID) -> str:
        """Admin-initiated reset: there is no email/SMS infrastructure in
        this desktop app, so the realistic mechanism is a random temporary
        password relayed out-of-band by whoever has users.manage — not an
        emailed reset link. Returned once; the target user must change it
        (must_change_password=True) before doing anything else — enforced
        in app.security.authorization, not left to the UI to remember.
        The audit entry deliberately never includes the temporary password
        itself.
        """
        temporary_password = secrets.token_urlsafe(12)
        self._users.update_password_hash(target_user_id, hash_password(temporary_password),
                                         must_change_password=True)
        self._audit(action="user.password_reset", entity_id=target_user_id)
        return temporary_password

    @require_permission("users.manage")
    def change_user_role(self, target_user_id: uuid.UUID, organization_id: uuid.UUID,
                        new_role_id: uuid.UUID) -> MembershipOut:
        existing = self._users.get_membership(target_user_id, organization_id)
        if existing is None:
            raise MembershipNotFoundError(target_user_id, organization_id)

        updated = self._users.update_membership_role(target_user_id, organization_id,
                                                      new_role_id)
        if updated is None:
            raise MembershipNotFoundError(target_user_id, organization_id)

        self._audit(action="user.role_changed", entity_id=target_user_id,
                   changes={"before": {"role_id": str(existing.role_id),
                                       "role_name": existing.role_name},
                           "after": {"role_id": str(updated.role_id),
                                    "role_name": updated.role_name}})
        return updated
