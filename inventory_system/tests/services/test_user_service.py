"""UserService tested against a fake UserRepository. The point of these
tests: proves users.manage is enforced *here*, in the service layer, not
merely assumed because a hypothetical UI wouldn't show the button — every
test calls the service directly, exactly as a UI bypass or a bug elsewhere
would.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.schemas.user import MembershipOut, UserOut
from app.security.authorization import PermissionDeniedError
from app.security.session import NotAuthenticatedError, SessionManager
from app.services.user_service import UserService

ORG_ID = uuid.uuid4()
ROLE_ID = uuid.uuid4()
OTHER_ROLE_ID = uuid.uuid4()
TARGET_USER_ID = uuid.uuid4()


class FakeUserRepository:
    def __init__(self):
        self.created = []
        self.active_state: dict[uuid.UUID, bool] = {TARGET_USER_ID: True}
        self.password_updates = []
        self.memberships: dict[tuple, MembershipOut] = {
            (TARGET_USER_ID, ORG_ID): MembershipOut(
                user_id=TARGET_USER_ID, organization_id=ORG_ID, role_id=ROLE_ID,
                role_name="SALES_STAFF", is_default=True),
        }

    def create_user(self, email, full_name, hashed_password, organization_id, role_id):
        self.created.append((email, full_name, hashed_password))
        now = datetime.now(timezone.utc)
        return UserOut(id=uuid.uuid4(), email=email, full_name=full_name, is_active=True,
                      is_superuser=False, must_change_password=False, created_at=now,
                      last_login_at=None)

    def set_active(self, user_id, is_active):
        self.active_state[user_id] = is_active

    def update_password_hash(self, user_id, new_hash, must_change_password=False):
        self.password_updates.append((user_id, new_hash, must_change_password))

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


def _service_as(permissions, repo=None):
    repo = repo or FakeUserRepository()
    sessions = SessionManager(idle_timeout=timedelta(minutes=30))
    sessions.start(user_id=uuid.uuid4(), organization_id=ORG_ID, role_id=ROLE_ID,
                   permissions=frozenset(permissions), is_superuser=False,
                   must_change_password=False, now=datetime.now(timezone.utc))
    audit_log = FakeAuditLogRepository()
    organizations = FakeOrganizationRepository()
    return UserService(repo, sessions, audit_log, organizations), repo, sessions, audit_log


def test_admin_with_users_manage_can_deactivate():
    service, repo, _, _ = _service_as({"users.manage"})
    service.deactivate_user(TARGET_USER_ID)
    assert repo.active_state[TARGET_USER_ID] is False


def test_viewer_without_users_manage_is_denied_deactivate():
    service, repo, _, _ = _service_as({"reports.view"})  # anything but users.manage
    with pytest.raises(PermissionDeniedError):
        service.deactivate_user(TARGET_USER_ID)
    assert repo.active_state[TARGET_USER_ID] is True  # unchanged


def test_denied_activate_does_not_touch_the_repository():
    service, repo, _, _ = _service_as(set())
    with pytest.raises(PermissionDeniedError):
        service.activate_user(TARGET_USER_ID)
    assert repo.active_state == {TARGET_USER_ID: True}


def test_denied_create_user_does_not_touch_the_repository():
    service, repo, _, _ = _service_as(set())
    with pytest.raises(PermissionDeniedError):
        service.create_user("new@acme.test", "New Person", "pass1234", ORG_ID, ROLE_ID)
    assert repo.created == []


def test_authorized_create_user_hashes_the_password_not_plaintext():
    service, repo, _, _ = _service_as({"users.manage"})
    service.create_user("new@acme.test", "New Person", "pass1234", ORG_ID, ROLE_ID)
    email, full_name, hashed = repo.created[0]
    assert email == "new@acme.test"
    assert hashed != "pass1234"
    assert hashed.startswith("$argon2id$")


def test_reset_password_requires_users_manage():
    service, repo, _, _ = _service_as(set())
    with pytest.raises(PermissionDeniedError):
        service.reset_password(TARGET_USER_ID)
    assert repo.password_updates == []


def test_reset_password_returns_a_one_time_temporary_password_and_forces_change():
    service, repo, _, _ = _service_as({"users.manage"})
    temp_password = service.reset_password(TARGET_USER_ID)
    assert isinstance(temp_password, str) and len(temp_password) >= 12
    user_id, new_hash, must_change = repo.password_updates[-1]
    assert user_id == TARGET_USER_ID
    assert must_change is True
    assert new_hash != temp_password  # never stores plaintext


def test_unauthenticated_call_raises_not_authenticated_not_permission_denied():
    repo = FakeUserRepository()
    sessions = SessionManager(idle_timeout=timedelta(minutes=30))
    service = UserService(repo, sessions, FakeAuditLogRepository(),
                          FakeOrganizationRepository())  # never logged in
    with pytest.raises(NotAuthenticatedError):
        service.deactivate_user(TARGET_USER_ID)
