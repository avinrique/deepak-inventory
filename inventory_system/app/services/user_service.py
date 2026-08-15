"""User administration: create/get/list/update, activate/deactivate,
admin-initiated password reset, role/permission assignment. Every method
here is a protected operation, gated by the specific users.* permission
that matches what it does (see app.security.permissions) — not one
umbrella "users.manage" catch-all, so a role can be given e.g. users.view
without also getting users.deactivate.

Field validation (app.domain.user.validate_user) and permission
enforcement (@require_permission) are both fully server-side — the same
checks run whether the caller is the real PySide6 UI or a test calling
this service directly. Repository methods pre-check for duplicates/
existence and this module raises UserValidationError/DuplicateEmailError/
DuplicateUsernameError/RoleNotFoundError/UserNotFoundError instead — a
raw IntegrityError/sqlalchemy exception should never reach a caller of
this service.

Every method that targets an existing user (get/update/activate/
deactivate/reset_password/change_user_role) verifies the target is a
member of the *calling admin's own organization* before touching them (via
UserRepository.get_membership / the membership check baked into
get_user/update_profile/set_active/update_password_hash) — without that
check, a users.*-equivalent holder in one organization could reach into a
different organization's users purely by guessing/enumerating a UUID.

An organization's Owner can't be deactivated or demoted through this
service (OwnerProtectedError) — the one-Owner-per-organization invariant
app.security.permissions documents is enforced here, not just asserted in
a comment.

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
import re
import secrets
import uuid
from datetime import datetime, timezone

from app.core.exceptions import (
    DuplicateEmailError,
    DuplicateUsernameError,
    MembershipNotFoundError,
    OwnerProtectedError,
    PasswordPolicyViolationError,
    RoleNotFoundError,
    UserNotFoundError,
    UserValidationError,
)
from app.domain.security_policy import PasswordPolicy, validate_password
from app.domain.user import normalize_email, normalize_username, validate_user
from app.repositories.interfaces import AuditLogRepository, OrganizationRepository, UserRepository
from app.schemas.user import MembershipOut, RoleOut, UserOut, UserSummaryOut, UserUpdate
from app.security.authorization import require_permission
from app.security.passwords import hash_password
from app.security.session import SessionManager

_OWNER_ROLE_NAME = "OWNER"
_USERNAME_SANITIZE_RE = re.compile(r"[^a-z0-9._-]+")
_MAX_USERNAME_SUFFIX_ATTEMPTS = 5


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

    def _organization_id(self) -> uuid.UUID:
        return self._sessions.current(now=datetime.now(timezone.utc)).organization_id

    def _audit(self, *, action: str, entity_id: uuid.UUID, changes: dict | None = None) -> None:
        actor_id, org_id = self._actor()
        self._audit_log.record(organization_id=org_id, user_id=actor_id, actor_email=None,
                               organization_name=None, action=action, entity_type="user",
                               entity_id=entity_id, changes=changes)

    def _require_membership(self, target_user_id: uuid.UUID,
                            organization_id: uuid.UUID) -> MembershipOut:
        membership = self._users.get_membership(target_user_id, organization_id)
        if membership is None:
            raise MembershipNotFoundError(target_user_id, organization_id)
        return membership

    def _derive_username(self, email: str) -> str:
        """Auto-generates a username when the caller doesn't supply one
        (every existing create_user call site predates this field). Not
        used when the caller explicitly chose a username — an explicit
        choice that collides is a DuplicateUsernameError, not silently
        suffixed into something the admin didn't type.
        """
        local_part = email.split("@", 1)[0].lower()
        base = _USERNAME_SANITIZE_RE.sub("-", local_part).strip("-") or "user"
        candidate = base
        for _ in range(_MAX_USERNAME_SUFFIX_ATTEMPTS):
            if not self._users.username_exists(candidate):
                return candidate
            candidate = f"{base}-{secrets.token_hex(2)}"
        # Astronomically unlikely with a 4-hex-char suffix space, but never
        # loop forever — surface the same error an explicit collision would.
        raise DuplicateUsernameError(candidate)

    @require_permission("users.create")
    def create_user(self, email: str, full_name: str, initial_password: str,
                    organization_id: uuid.UUID, role_id: uuid.UUID,
                    username: str | None = None, phone: str | None = None) -> UserOut:
        if not self._users.role_exists(role_id):
            raise RoleNotFoundError(role_id)

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

        email = normalize_email(email)
        # An explicit username is validated/checked as typed; an omitted
        # one is derived (and therefore already valid/unique by
        # construction) below, after the shared field validation — see
        # _derive_username's docstring for why it isn't re-validated here.
        explicit_username = normalize_username(username) if username is not None else None
        errors = validate_user(full_name=full_name, email=email,
                               username=explicit_username, phone=phone)
        if errors:
            raise UserValidationError(errors)

        if self._users.email_exists(email):
            raise DuplicateEmailError(email)
        if explicit_username is not None:
            if self._users.username_exists(explicit_username):
                raise DuplicateUsernameError(explicit_username)
            username = explicit_username
        else:
            username = self._derive_username(email)

        created = self._users.create_user(email=email, full_name=full_name,
                                          hashed_password=hash_password(initial_password),
                                          organization_id=organization_id, role_id=role_id,
                                          username=username, phone=phone)
        self._audit(action="user.create", entity_id=created.id,
                   changes={"email": created.email, "username": created.username,
                           "full_name": created.full_name,
                           "organization_id": str(organization_id), "role_id": str(role_id)})
        return created

    @require_permission("users.view")
    def list_users(self) -> list[UserSummaryOut]:
        return self._users.list_users(self._organization_id())

    @require_permission("users.view")
    def get_user(self, target_user_id: uuid.UUID) -> UserSummaryOut:
        organization_id = self._organization_id()
        summary = self._users.get_user(target_user_id, organization_id)
        if summary is None:
            raise MembershipNotFoundError(target_user_id, organization_id)
        return summary

    @require_permission("users.view")
    def list_roles(self) -> list[RoleOut]:
        return self._users.list_roles()

    @require_permission("users.update")
    def update_user(self, target_user_id: uuid.UUID, data: UserUpdate) -> UserOut:
        organization_id = self._organization_id()
        self._require_membership(target_user_id, organization_id)
        current = self._users.get_user(target_user_id, organization_id)
        if current is None:  # membership existed a moment ago but the user row is gone
            raise UserNotFoundError(target_user_id)

        updates = data.model_dump(exclude_unset=True)
        email = normalize_email(updates["email"]) if "email" in updates else current.email
        username = (normalize_username(updates["username"]) if "username" in updates
                   else current.username)
        full_name = updates.get("full_name", current.full_name)
        phone = updates.get("phone", current.phone)

        errors = validate_user(full_name=full_name, email=email, username=username, phone=phone)
        if errors:
            raise UserValidationError(errors)
        if email != current.email and self._users.email_exists(
                email, exclude_user_id=target_user_id):
            raise DuplicateEmailError(email)
        if username != current.username and self._users.username_exists(
                username, exclude_user_id=target_user_id):
            raise DuplicateUsernameError(username)

        if "email" in updates:
            updates["email"] = email
        if "username" in updates:
            updates["username"] = username
        result = self._users.update_profile(target_user_id, organization_id,
                                            UserUpdate(**updates))
        if result is None:
            raise UserNotFoundError(target_user_id)

        # Only the fields that actually changed — same before/after diff
        # shape as ProductService.update_product's audit entry.
        before, after = {}, {}
        for field in updates:
            old_value = getattr(current, field)
            new_value = getattr(result, field)
            if old_value != new_value:
                before[field] = str(old_value)
                after[field] = str(new_value)
        if before:
            self._audit(action="user.update", entity_id=target_user_id,
                       changes={"before": before, "after": after})
        return result

    @require_permission("users.update")
    def activate_user(self, target_user_id: uuid.UUID) -> None:
        organization_id = self._organization_id()
        self._require_membership(target_user_id, organization_id)
        if not self._users.set_active(target_user_id, organization_id, True):
            raise UserNotFoundError(target_user_id)
        self._audit(action="user.activate", entity_id=target_user_id)

    @require_permission("users.deactivate")
    def deactivate_user(self, target_user_id: uuid.UUID) -> None:
        organization_id = self._organization_id()
        membership = self._require_membership(target_user_id, organization_id)
        if membership.role_name == _OWNER_ROLE_NAME:
            raise OwnerProtectedError(target_user_id)
        if not self._users.set_active(target_user_id, organization_id, False):
            raise UserNotFoundError(target_user_id)
        self._audit(action="user.deactivate", entity_id=target_user_id)

    @require_permission("users.reset_password")
    def reset_password(self, target_user_id: uuid.UUID) -> str:
        """Admin-initiated reset: there is no email/SMS infrastructure in
        this desktop app, so the realistic mechanism is a random temporary
        password relayed out-of-band by whoever has users.reset_password —
        not an emailed reset link. Returned once; the target user must
        change it (must_change_password=True) before doing anything else —
        enforced in app.security.authorization, not left to the UI to
        remember. The audit entry deliberately never includes the
        temporary password itself.
        """
        organization_id = self._organization_id()
        self._require_membership(target_user_id, organization_id)
        temporary_password = secrets.token_urlsafe(12)
        if not self._users.update_password_hash(target_user_id, organization_id,
                                                 hash_password(temporary_password),
                                                 must_change_password=True):
            raise UserNotFoundError(target_user_id)
        self._audit(action="user.password_reset", entity_id=target_user_id)
        return temporary_password

    @require_permission("users.manage_roles")
    def change_user_role(self, target_user_id: uuid.UUID,
                        new_role_id: uuid.UUID) -> MembershipOut:
        # organization_id always comes from the caller's own session, never
        # a parameter — otherwise a caller could name a *different*
        # organization's id here and reassign roles for a user who isn't
        # even a member of the org the caller actually has
        # users.manage_roles in. See the module docstring.
        organization_id = self._organization_id()
        existing = self._require_membership(target_user_id, organization_id)
        if existing.role_name == _OWNER_ROLE_NAME:
            raise OwnerProtectedError(target_user_id)
        if not self._users.role_exists(new_role_id):
            raise RoleNotFoundError(new_role_id)

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
