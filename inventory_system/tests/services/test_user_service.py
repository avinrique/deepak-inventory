"""UserService tested against a fake UserRepository. The point of these
tests: proves each users.* permission is enforced *here*, in the service
layer, not merely assumed because a hypothetical UI wouldn't show the
button — every test calls the service directly, exactly as a UI bypass or
a bug elsewhere would. Also covers the organization-membership guard
(activate/deactivate/reset_password/change_user_role all refuse to touch a
user who isn't a member of the caller's own organization) and the
Owner-protection guard (an org's Owner can't be deactivated or demoted).
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.core.exceptions import (
    DuplicateEmailError,
    DuplicateUsernameError,
    MembershipNotFoundError,
    OwnerProtectedError,
    RoleNotFoundError,
    UserNotFoundError,
    UserValidationError,
)
from app.schemas.user import MembershipOut, RoleOut, UserOut, UserSummaryOut, UserUpdate
from app.security.authorization import PermissionDeniedError
from app.security.session import NotAuthenticatedError, SessionManager
from app.services.user_service import UserService

ORG_ID = uuid.uuid4()
OTHER_ORG_ID = uuid.uuid4()
ROLE_ID = uuid.uuid4()
OTHER_ROLE_ID = uuid.uuid4()
OWNER_ROLE_ID = uuid.uuid4()
TARGET_USER_ID = uuid.uuid4()
OWNER_USER_ID = uuid.uuid4()
OUTSIDER_USER_ID = uuid.uuid4()  # a member of OTHER_ORG_ID, not ORG_ID

ALL_USERS_PERMISSIONS = frozenset({
    "users.view", "users.create", "users.update", "users.deactivate",
    "users.reset_password", "users.manage_roles",
})


class FakeUserRepository:
    _KNOWN_ROLE_IDS = {ROLE_ID, OTHER_ROLE_ID, OWNER_ROLE_ID}

    def __init__(self):
        self.created = []
        self.active_state: dict[uuid.UUID, bool] = {TARGET_USER_ID: True, OWNER_USER_ID: True}
        self.password_updates = []
        # Simulates "membership row exists but the user row itself is
        # gone" — an edge case set_active/update_password_hash's False
        # return covers even though it can't happen in the real schema
        # (users.id cascades to user_organizations), same as
        # ProductRepository.get_by_id returning None being tested even
        # though a live FK would normally prevent it.
        self.missing_user_rows: set[uuid.UUID] = set()
        self.memberships: dict[tuple, MembershipOut] = {
            (TARGET_USER_ID, ORG_ID): MembershipOut(
                user_id=TARGET_USER_ID, organization_id=ORG_ID, role_id=ROLE_ID,
                role_name="SALES_STAFF", is_default=True),
            (OWNER_USER_ID, ORG_ID): MembershipOut(
                user_id=OWNER_USER_ID, organization_id=ORG_ID, role_id=OWNER_ROLE_ID,
                role_name="OWNER", is_default=True),
            (OUTSIDER_USER_ID, OTHER_ORG_ID): MembershipOut(
                user_id=OUTSIDER_USER_ID, organization_id=OTHER_ORG_ID, role_id=ROLE_ID,
                role_name="SALES_STAFF", is_default=True),
        }
        # The actual profile store — list_users/get_user/update_profile all
        # read/write through this, exactly like the real repository reads/
        # writes through the users table.
        now = datetime.now(timezone.utc)
        self.profiles: dict[uuid.UUID, dict] = {
            TARGET_USER_ID: {"email": "target@acme.test", "username": "target",
                            "full_name": "Target Person", "phone": None,
                            "is_superuser": False, "must_change_password": False,
                            "created_at": now, "last_login_at": None},
            OWNER_USER_ID: {"email": "owner@acme.test", "username": "owner",
                           "full_name": "Owner Person", "phone": None,
                           "is_superuser": False, "must_change_password": False,
                           "created_at": now, "last_login_at": None},
            OUTSIDER_USER_ID: {"email": "outsider@other.test", "username": "outsider",
                              "full_name": "Outsider Person", "phone": None,
                              "is_superuser": False, "must_change_password": False,
                              "created_at": now, "last_login_at": None},
        }

    def _summary(self, user_id, organization_id):
        membership = self.memberships.get((user_id, organization_id))
        profile = self.profiles.get(user_id)
        if membership is None or profile is None:
            return None
        return UserSummaryOut(id=user_id, email=profile["email"], username=profile["username"],
                              full_name=profile["full_name"], phone=profile["phone"],
                              is_active=self.active_state.get(user_id, True),
                              is_superuser=profile["is_superuser"],
                              must_change_password=profile["must_change_password"],
                              role_id=membership.role_id, role_name=membership.role_name,
                              created_at=profile["created_at"],
                              last_login_at=profile["last_login_at"])

    def create_user(self, email, full_name, hashed_password, organization_id, role_id,
                    username, phone=None, is_active=True):
        self.created.append((email, full_name, hashed_password, username, phone))
        user_id = uuid.uuid4()
        now = datetime.now(timezone.utc)
        self.profiles[user_id] = {"email": email, "username": username,
                                  "full_name": full_name, "phone": phone,
                                  "is_superuser": False, "must_change_password": False,
                                  "created_at": now, "last_login_at": None}
        self.active_state[user_id] = is_active
        self.memberships[(user_id, organization_id)] = MembershipOut(
            user_id=user_id, organization_id=organization_id, role_id=role_id,
            role_name="SALES_STAFF", is_default=True)
        return UserOut(id=user_id, email=email, username=username, full_name=full_name,
                      phone=phone, is_active=is_active, is_superuser=False,
                      must_change_password=False, created_at=now, last_login_at=None)

    def email_exists(self, email, exclude_user_id=None):
        return any(p["email"] == email for uid, p in self.profiles.items()
                  if uid != exclude_user_id)

    def username_exists(self, username, exclude_user_id=None):
        return any(p["username"] == username for uid, p in self.profiles.items()
                  if uid != exclude_user_id)

    def list_users(self, organization_id):
        return [self._summary(uid, org) for (uid, org) in self.memberships
               if org == organization_id]

    def get_user(self, user_id, organization_id):
        return self._summary(user_id, organization_id)

    def update_profile(self, user_id, organization_id, data):
        if (user_id, organization_id) not in self.memberships:
            return None
        if user_id in self.missing_user_rows:
            return None
        profile = self.profiles[user_id]
        for field in ("email", "username", "full_name", "phone"):
            value = getattr(data, field)
            if value is not None:
                profile[field] = value
        return UserOut(id=user_id, email=profile["email"], username=profile["username"],
                      full_name=profile["full_name"], phone=profile["phone"],
                      is_active=self.active_state.get(user_id, True),
                      is_superuser=profile["is_superuser"],
                      must_change_password=profile["must_change_password"],
                      created_at=profile["created_at"], last_login_at=profile["last_login_at"])

    def list_roles(self):
        return [RoleOut(id=ROLE_ID, name="SALES_STAFF", description=None, is_system=True)]

    def role_exists(self, role_id):
        return role_id in self._KNOWN_ROLE_IDS

    def set_active(self, user_id, organization_id, is_active):
        if (user_id, organization_id) not in self.memberships:
            return False
        if user_id in self.missing_user_rows:
            return False
        self.active_state[user_id] = is_active
        return True

    def update_password_hash(self, user_id, organization_id, new_hash,
                             must_change_password=False):
        if (user_id, organization_id) not in self.memberships:
            return False
        if user_id in self.missing_user_rows:
            return False
        self.password_updates.append((user_id, new_hash, must_change_password))
        return True

    def get_membership(self, user_id, organization_id):
        return self.memberships.get((user_id, organization_id))

    def update_membership_role(self, user_id, organization_id, role_id):
        existing = self.memberships.get((user_id, organization_id))
        if existing is None:
            return None
        role_names = {ROLE_ID: "SALES_STAFF", OTHER_ROLE_ID: "MANAGER"}
        updated = existing.model_copy(update={"role_id": role_id,
                                              "role_name": role_names.get(role_id, "UNKNOWN")})
        self.memberships[(user_id, organization_id)] = updated
        return updated

    # unused by UserService but required by the Protocol shape in spirit
    def get_by_id(self, user_id): raise NotImplementedError
    def get_credentials_by_email(self, email): raise NotImplementedError
    def get_credentials_by_id(self, user_id): raise NotImplementedError
    def list_memberships(self, user_id): raise NotImplementedError
    def get_role_permissions(self, role_id): raise NotImplementedError
    def clear_must_change_password(self, user_id): raise NotImplementedError
    def record_login(self, user_id, when): raise NotImplementedError


class FakeAuditLogRepository:
    def __init__(self):
        self.entries: list[dict] = []

    def record(self, *, organization_id, user_id, actor_email, organization_name, action,
              entity_type=None, entity_id=None, changes=None):
        self.entries.append({"organization_id": organization_id, "user_id": user_id,
                            "actor_email": actor_email, "organization_name": organization_name,
                            "action": action, "entity_type": entity_type,
                            "entity_id": entity_id, "changes": changes})


class FakeOrganizationRepository:
    """Empty by default — UserService.create_user treats a missing
    organization as "no password policy to enforce", a no-op unless a test
    explicitly seeds one via .orgs[ORG_ID] = OrganizationOut(...).
    """
    def __init__(self):
        self.orgs: dict[uuid.UUID, object] = {}

    def get_by_id(self, organization_id):
        return self.orgs.get(organization_id)

    def update(self, organization_id, data):
        raise NotImplementedError

    def get_logo(self, organization_id):
        raise NotImplementedError


def _service_as(permissions, repo=None, organization_id=ORG_ID):
    repo = repo or FakeUserRepository()
    sessions = SessionManager(idle_timeout=timedelta(minutes=30))
    sessions.start(user_id=uuid.uuid4(), organization_id=organization_id, role_id=ROLE_ID,
                   permissions=frozenset(permissions), is_superuser=False,
                   must_change_password=False, now=datetime.now(timezone.utc))
    audit_log = FakeAuditLogRepository()
    organizations = FakeOrganizationRepository()
    return UserService(repo, sessions, audit_log, organizations), repo, sessions, audit_log


# -- create_user ------------------------------------------------------------#

def test_denied_create_user_does_not_touch_the_repository():
    service, repo, _, _ = _service_as(set())
    with pytest.raises(PermissionDeniedError):
        service.create_user("new@acme.test", "New Person", "pass1234", ORG_ID, ROLE_ID)
    assert repo.created == []


def test_authorized_create_user_hashes_the_password_not_plaintext():
    service, repo, _, _ = _service_as({"users.create"})
    service.create_user("new@acme.test", "New Person", "pass1234", ORG_ID, ROLE_ID)
    email, full_name, hashed, username, phone = repo.created[0]
    assert email == "new@acme.test"
    assert hashed != "pass1234"
    assert hashed.startswith("$argon2id$")


def test_create_user_without_a_username_derives_one_from_the_email():
    service, repo, _, _ = _service_as({"users.create"})
    service.create_user("new.person@acme.test", "New Person", "pass1234", ORG_ID, ROLE_ID)
    _email, _full_name, _hashed, username, _phone = repo.created[0]
    assert username == "new.person"


def test_create_user_rejects_duplicate_email():
    service, repo, _, _ = _service_as({"users.create"})
    with pytest.raises(DuplicateEmailError):
        # target@acme.test is already seeded on TARGET_USER_ID
        service.create_user("target@acme.test", "New Person", "pass1234", ORG_ID, ROLE_ID)


def test_create_user_rejects_an_explicitly_chosen_duplicate_username():
    service, repo, _, _ = _service_as({"users.create"})
    with pytest.raises(DuplicateUsernameError):
        # "target" is already seeded as TARGET_USER_ID's username
        service.create_user("new@acme.test", "New Person", "pass1234", ORG_ID, ROLE_ID,
                            username="target")


def test_create_user_rejects_invalid_email():
    service, repo, _, _ = _service_as({"users.create"})
    with pytest.raises(UserValidationError):
        service.create_user("not-an-email", "New Person", "pass1234", ORG_ID, ROLE_ID)


def test_create_user_rejects_unknown_role():
    service, repo, _, _ = _service_as({"users.create"})
    with pytest.raises(RoleNotFoundError):
        service.create_user("new@acme.test", "New Person", "pass1234", ORG_ID,
                            uuid.uuid4())
    assert repo.created == []


# -- list_users / list_roles -------------------------------------------------#

def test_list_users_requires_users_view():
    service, _, _, _ = _service_as(set())
    with pytest.raises(PermissionDeniedError):
        service.list_users()


def test_list_users_scoped_to_the_callers_organization():
    service, _, _, _ = _service_as({"users.view"})
    result = service.list_users()
    assert {u.id for u in result} == {TARGET_USER_ID, OWNER_USER_ID}


def test_list_roles_requires_users_view():
    service, _, _, _ = _service_as(set())
    with pytest.raises(PermissionDeniedError):
        service.list_roles()


# -- get_user ------------------------------------------------------------#

def test_get_user_requires_users_view():
    service, _, _, _ = _service_as(set())
    with pytest.raises(PermissionDeniedError):
        service.get_user(TARGET_USER_ID)


def test_get_user_returns_the_full_profile():
    service, _, _, _ = _service_as({"users.view"})
    result = service.get_user(TARGET_USER_ID)
    assert result.email == "target@acme.test"
    assert result.role_name == "SALES_STAFF"


def test_get_user_outside_the_callers_org_raises_membership_not_found():
    service, _, _, _ = _service_as({"users.view"})
    with pytest.raises(MembershipNotFoundError):
        service.get_user(OUTSIDER_USER_ID)


# -- update_user -----------------------------------------------------------#

def test_update_user_requires_users_update():
    service, _, _, _ = _service_as(set())
    with pytest.raises(PermissionDeniedError):
        service.update_user(TARGET_USER_ID, UserUpdate(full_name="New Name"))


def test_update_user_applies_only_the_fields_that_were_set():
    service, repo, _, audit_log = _service_as({"users.update"})
    result = service.update_user(TARGET_USER_ID, UserUpdate(full_name="New Name"))
    assert result.full_name == "New Name"
    assert result.email == "target@acme.test"  # untouched
    assert any(e["action"] == "user.update" for e in audit_log.entries)


def test_update_user_rejects_duplicate_email():
    service, repo, _, _ = _service_as({"users.update"})
    with pytest.raises(DuplicateEmailError):
        # owner@acme.test belongs to a different user (OWNER_USER_ID)
        service.update_user(TARGET_USER_ID, UserUpdate(email="owner@acme.test"))


def test_update_user_allows_keeping_its_own_current_email():
    service, repo, _, _ = _service_as({"users.update"})
    result = service.update_user(TARGET_USER_ID, UserUpdate(email="target@acme.test"))
    assert result.email == "target@acme.test"


def test_update_user_rejects_invalid_phone():
    service, repo, _, _ = _service_as({"users.update"})
    with pytest.raises(UserValidationError):
        service.update_user(TARGET_USER_ID, UserUpdate(phone="not a phone number!!"))


def test_update_user_outside_the_callers_org_raises_membership_not_found():
    service, repo, _, _ = _service_as({"users.update"})
    with pytest.raises(MembershipNotFoundError):
        service.update_user(OUTSIDER_USER_ID, UserUpdate(full_name="Hacked"))
    assert repo.profiles[OUTSIDER_USER_ID]["full_name"] == "Outsider Person"  # unchanged


# -- activate/deactivate -----------------------------------------------------#

def test_admin_with_users_update_can_activate():
    service, repo, _, _ = _service_as({"users.update"})
    repo.active_state[TARGET_USER_ID] = False
    service.activate_user(TARGET_USER_ID)
    assert repo.active_state[TARGET_USER_ID] is True


def test_admin_with_users_deactivate_can_deactivate():
    service, repo, _, _ = _service_as({"users.deactivate"})
    service.deactivate_user(TARGET_USER_ID)
    assert repo.active_state[TARGET_USER_ID] is False


def test_viewer_without_permission_is_denied_deactivate():
    service, repo, _, _ = _service_as({"reports.view"})  # anything but users.deactivate
    with pytest.raises(PermissionDeniedError):
        service.deactivate_user(TARGET_USER_ID)
    assert repo.active_state[TARGET_USER_ID] is True  # unchanged


def test_denied_activate_does_not_touch_the_repository():
    service, repo, _, _ = _service_as(set())
    with pytest.raises(PermissionDeniedError):
        service.activate_user(TARGET_USER_ID)
    assert repo.active_state == {TARGET_USER_ID: True, OWNER_USER_ID: True}


def test_deactivate_a_user_who_is_not_a_member_of_the_callers_org_raises_membership_not_found():
    service, repo, _, _ = _service_as({"users.deactivate"})
    with pytest.raises(MembershipNotFoundError):
        service.deactivate_user(OUTSIDER_USER_ID)
    # the cross-tenant write never reached the repository at all
    assert OUTSIDER_USER_ID not in repo.active_state


def test_reset_password_on_a_user_outside_the_callers_org_raises_membership_not_found():
    service, repo, _, _ = _service_as({"users.reset_password"})
    with pytest.raises(MembershipNotFoundError):
        service.reset_password(OUTSIDER_USER_ID)
    assert repo.password_updates == []


def test_deactivate_the_organizations_owner_is_refused():
    service, repo, _, _ = _service_as({"users.deactivate"})
    with pytest.raises(OwnerProtectedError):
        service.deactivate_user(OWNER_USER_ID)
    assert repo.active_state[OWNER_USER_ID] is True


def test_change_role_of_the_organizations_owner_is_refused():
    service, repo, _, _ = _service_as({"users.manage_roles"})
    with pytest.raises(OwnerProtectedError):
        service.change_user_role(OWNER_USER_ID, OTHER_ROLE_ID)
    assert repo.memberships[(OWNER_USER_ID, ORG_ID)].role_name == "OWNER"


# -- reset_password -----------------------------------------------------------#

def test_reset_password_requires_users_reset_password():
    service, repo, _, _ = _service_as(set())
    with pytest.raises(PermissionDeniedError):
        service.reset_password(TARGET_USER_ID)
    assert repo.password_updates == []


def test_reset_password_returns_a_one_time_temporary_password_and_forces_change():
    service, repo, _, _ = _service_as({"users.reset_password"})
    temp_password = service.reset_password(TARGET_USER_ID)
    assert isinstance(temp_password, str) and len(temp_password) >= 12
    user_id, new_hash, must_change = repo.password_updates[-1]
    assert user_id == TARGET_USER_ID
    assert must_change is True
    assert new_hash != temp_password  # never stores plaintext


def test_reset_password_on_missing_user_raises_user_not_found():
    service, repo, _, _ = _service_as({"users.reset_password"})
    repo.missing_user_rows.add(TARGET_USER_ID)  # membership row exists, user row doesn't
    with pytest.raises(UserNotFoundError):
        service.reset_password(TARGET_USER_ID)


# -- change_user_role ---------------------------------------------------------#

def test_change_user_role_requires_users_manage_roles():
    service, repo, _, _ = _service_as(set())
    with pytest.raises(PermissionDeniedError):
        service.change_user_role(TARGET_USER_ID, OTHER_ROLE_ID)


def test_change_user_role_ignores_any_other_organization_than_the_callers_own():
    # Even though OUTSIDER_USER_ID is a real member of OTHER_ORG_ID, the
    # caller's session is scoped to ORG_ID — organization_id is derived
    # from the session, not accepted as an argument, so there is no way to
    # point this call at a different tenant.
    service, repo, _, _ = _service_as({"users.manage_roles"}, organization_id=ORG_ID)
    with pytest.raises(MembershipNotFoundError):
        service.change_user_role(OUTSIDER_USER_ID, OTHER_ROLE_ID)


def test_authorized_change_user_role_updates_and_audits():
    service, repo, _, audit_log = _service_as({"users.manage_roles"})
    updated = service.change_user_role(TARGET_USER_ID, OTHER_ROLE_ID)
    assert updated.role_name == "MANAGER"
    assert any(e["action"] == "user.role_changed" for e in audit_log.entries)


def test_change_user_role_rejects_an_unknown_role_id():
    service, repo, _, _ = _service_as({"users.manage_roles"})
    with pytest.raises(RoleNotFoundError):
        service.change_user_role(TARGET_USER_ID, uuid.uuid4())
    # unchanged
    assert repo.memberships[(TARGET_USER_ID, ORG_ID)].role_name == "SALES_STAFF"


# -- unauthenticated -----------------------------------------------------------#

def test_unauthenticated_call_raises_not_authenticated_not_permission_denied():
    repo = FakeUserRepository()
    sessions = SessionManager(idle_timeout=timedelta(minutes=30))
    service = UserService(repo, sessions, FakeAuditLogRepository(),
                          FakeOrganizationRepository())  # never logged in
    with pytest.raises(NotAuthenticatedError):
        service.deactivate_user(TARGET_USER_ID)
