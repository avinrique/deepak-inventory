"""AuthService tested against a hand-written fake UserRepository — no
database. Proves login/logout/change-password and session-timeout
interplay without needing Postgres.
"""
import uuid
from datetime import timedelta

import pytest

from app.core.exceptions import AmbiguousOrganizationError, InvalidCredentialsError
from app.schemas.user import MembershipOut, UserCredentials
from app.security.authorization import require_permission
from app.security.passwords import hash_password
from app.security.session import NotAuthenticatedError, SessionExpiredError, SessionManager
from app.services.auth_service import AuthService

ORG_ID = uuid.uuid4()
OTHER_ORG_ID = uuid.uuid4()
ROLE_ID = uuid.uuid4()


class FakeUserRepository:
    def __init__(self):
        self.users: dict[uuid.UUID, UserCredentials] = {}
        self.memberships: dict[uuid.UUID, list[MembershipOut]] = {}
        self.role_permissions: dict[uuid.UUID, frozenset[str]] = {}
        self.logins: list[tuple[uuid.UUID, object]] = []
        self.password_updates: list[tuple[uuid.UUID, uuid.UUID | None, str, bool]] = []

    def seed_user(self, email, password, *, is_active=True, is_superuser=False,
                 must_change_password=False, memberships=()):
        user_id = uuid.uuid4()
        self.users[user_id] = UserCredentials(
            id=user_id, email=email, hashed_password=hash_password(password),
            is_active=is_active, is_superuser=is_superuser,
            must_change_password=must_change_password)
        self.memberships[user_id] = list(memberships)
        return user_id

    # --- UserRepository protocol -----------------------------------
    def get_by_id(self, user_id):
        raise NotImplementedError

    def get_credentials_by_email(self, email):
        email = email.strip().lower()
        for creds in self.users.values():
            if creds.email == email:
                return creds
        return None

    def get_credentials_by_id(self, user_id):
        return self.users.get(user_id)

    def get_membership(self, user_id, organization_id):
        return next((m for m in self.memberships.get(user_id, [])
                    if m.organization_id == organization_id), None)

    def list_memberships(self, user_id):
        return self.memberships.get(user_id, [])

    def get_role_permissions(self, role_id):
        return self.role_permissions.get(role_id, frozenset())

    def create_user(self, **kwargs):
        raise NotImplementedError

    def set_active(self, user_id, is_active):
        raise NotImplementedError

    def update_password_hash(self, user_id, organization_id, new_hash,
                             must_change_password=False):
        self.password_updates.append((user_id, organization_id, new_hash, must_change_password))
        old = self.users[user_id]
        self.users[user_id] = old.model_copy(
            update={"hashed_password": new_hash,
                   "must_change_password": must_change_password})

    def clear_must_change_password(self, user_id):
        old = self.users[user_id]
        self.users[user_id] = old.model_copy(update={"must_change_password": False})

    def record_login(self, user_id, when):
        self.logins.append((user_id, when))


def _membership(user_id, org_id=ORG_ID, role_id=ROLE_ID, is_default=True):
    return MembershipOut(user_id=user_id, organization_id=org_id, role_id=role_id,
                         role_name="MANAGER", is_default=is_default)


class FakeOrganizationRepository:
    """Empty by default — AuthService treats a missing organization as "no
    Settings to apply", so login's session-timeout push and
    change_password's password-policy check are both no-ops unless a test
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


class FakeAuditLogRepository:
    def __init__(self):
        self.entries: list[dict] = []

    def record(self, *, organization_id, user_id, actor_email, organization_name, action,
              entity_type=None, entity_id=None, changes=None):
        self.entries.append({"organization_id": organization_id, "user_id": user_id,
                            "actor_email": actor_email, "organization_name": organization_name,
                            "action": action, "entity_type": entity_type,
                            "entity_id": entity_id, "changes": changes})


def _service(repo=None, sessions=None, audit_log=None, organizations=None):
    repo = repo or FakeUserRepository()
    sessions = sessions or SessionManager(idle_timeout=timedelta(minutes=30))
    audit_log = audit_log or FakeAuditLogRepository()
    organizations = organizations or FakeOrganizationRepository()
    return AuthService(repo, sessions, audit_log, organizations), repo, sessions, audit_log


def test_login_with_correct_password_succeeds():
    repo = FakeUserRepository()
    user_id = repo.seed_user("owner@acme.test", "s3cret!")
    repo.memberships[user_id] = [_membership(user_id)]
    repo.role_permissions[ROLE_ID] = frozenset({"sales.create"})
    service, repo, sessions, _ = _service(repo)

    session = service.login("owner@acme.test", "s3cret!")

    assert session.user_id == user_id
    assert session.organization_id == ORG_ID
    assert session.permissions == frozenset({"sales.create"})
    assert sessions.is_authenticated is True
    assert repo.logins and repo.logins[0][0] == user_id


def test_login_is_case_insensitive_and_trims_email():
    repo = FakeUserRepository()
    user_id = repo.seed_user("owner@acme.test", "s3cret!")
    repo.memberships[user_id] = [_membership(user_id)]
    service, _, _, _ = _service(repo)
    session = service.login("  Owner@Acme.TEST  ", "s3cret!")
    assert session.user_id == user_id


def test_login_wrong_password_rejected():
    repo = FakeUserRepository()
    repo.seed_user("owner@acme.test", "s3cret!")
    service, repo, sessions, _ = _service(repo)
    with pytest.raises(InvalidCredentialsError):
        service.login("owner@acme.test", "wrong")
    assert sessions.is_authenticated is False


def test_login_unknown_email_rejected_generically():
    service, _, sessions, _ = _service()
    with pytest.raises(InvalidCredentialsError):
        service.login("nobody@acme.test", "whatever")
    assert sessions.is_authenticated is False


def test_login_inactive_user_rejected():
    repo = FakeUserRepository()
    repo.seed_user("owner@acme.test", "s3cret!", is_active=False)
    service, _, sessions, _ = _service(repo)
    with pytest.raises(InvalidCredentialsError):
        service.login("owner@acme.test", "s3cret!")
    assert sessions.is_authenticated is False


def test_login_ambiguous_organization_without_default_is_rejected():
    repo = FakeUserRepository()
    user_id = repo.seed_user("owner@acme.test", "s3cret!")
    repo.memberships[user_id] = [
        _membership(user_id, org_id=ORG_ID, is_default=False),
        _membership(user_id, org_id=OTHER_ORG_ID, is_default=False),
    ]
    service, _, _, _ = _service(repo)
    with pytest.raises(AmbiguousOrganizationError):
        service.login("owner@acme.test", "s3cret!")


def test_login_picks_default_membership_when_multiple_orgs():
    repo = FakeUserRepository()
    user_id = repo.seed_user("owner@acme.test", "s3cret!")
    repo.memberships[user_id] = [
        _membership(user_id, org_id=ORG_ID, is_default=False),
        _membership(user_id, org_id=OTHER_ORG_ID, is_default=True),
    ]
    service, _, _, _ = _service(repo)
    session = service.login("owner@acme.test", "s3cret!")
    assert session.organization_id == OTHER_ORG_ID


def test_login_explicit_organization_id_overrides_default():
    repo = FakeUserRepository()
    user_id = repo.seed_user("owner@acme.test", "s3cret!")
    repo.memberships[user_id] = [
        _membership(user_id, org_id=ORG_ID, is_default=True),
        _membership(user_id, org_id=OTHER_ORG_ID, is_default=False),
    ]
    service, _, _, _ = _service(repo)
    session = service.login("owner@acme.test", "s3cret!", organization_id=OTHER_ORG_ID)
    assert session.organization_id == OTHER_ORG_ID


def test_login_superuser_with_no_memberships_still_succeeds():
    repo = FakeUserRepository()
    repo.seed_user("root@acme.test", "s3cret!", is_superuser=True)
    service, _, sessions, _ = _service(repo)
    session = service.login("root@acme.test", "s3cret!")
    assert session.is_superuser is True
    assert session.organization_id is None


def test_login_carries_must_change_password_into_session():
    repo = FakeUserRepository()
    repo.seed_user("owner@acme.test", "temp-pass", must_change_password=True)
    service, _, _, _ = _service(repo)
    session = service.login("owner@acme.test", "temp-pass")
    assert session.must_change_password is True


def test_logout_ends_the_session():
    repo = FakeUserRepository()
    repo.seed_user("owner@acme.test", "s3cret!")
    service, _, sessions, _ = _service(repo)
    service.login("owner@acme.test", "s3cret!")
    service.logout()
    assert sessions.is_authenticated is False


def test_change_password_with_correct_old_password_succeeds():
    repo = FakeUserRepository()
    user_id = repo.seed_user("owner@acme.test", "old-pass")
    service, repo, sessions, _ = _service(repo)
    service.login("owner@acme.test", "old-pass")

    service.change_password("old-pass", "new-pass")

    assert repo.password_updates[-1][0] == user_id
    assert repo.password_updates[-1][3] is False  # must_change_password cleared
    # new password now works for a fresh login
    sessions.end()
    service.login("owner@acme.test", "new-pass")


def test_change_password_with_wrong_old_password_rejected():
    repo = FakeUserRepository()
    repo.seed_user("owner@acme.test", "old-pass")
    service, _, _, _ = _service(repo)
    service.login("owner@acme.test", "old-pass")
    with pytest.raises(InvalidCredentialsError):
        service.change_password("wrong-old-pass", "new-pass")


def test_change_password_requires_a_session():
    service, _, _, _ = _service()
    with pytest.raises(NotAuthenticatedError):
        service.change_password("anything", "new-pass")


def test_change_password_clears_must_change_password_flag():
    repo = FakeUserRepository()
    repo.seed_user("owner@acme.test", "temp-pass", must_change_password=True)
    service, _, sessions, _ = _service(repo)
    session = service.login("owner@acme.test", "temp-pass")
    assert session.must_change_password is True

    service.change_password("temp-pass", "new-pass")

    assert sessions.current(now=session.last_activity_at).must_change_password is False


def test_login_after_idle_timeout_requires_relogin_for_protected_calls():
    repo = FakeUserRepository()
    user_id = repo.seed_user("owner@acme.test", "s3cret!")
    repo.memberships[user_id] = [_membership(user_id)]
    repo.role_permissions[ROLE_ID] = frozenset({"sales.create"})
    sessions = SessionManager(idle_timeout=timedelta(minutes=30))
    service, repo, sessions, _ = _service(repo, sessions)
    service.login("owner@acme.test", "s3cret!")

    sessions._session.last_activity_at -= timedelta(hours=1)  # noqa: SLF001 - simulate idle

    class Protected:
        def __init__(self, sessions):
            self._sessions = sessions

        @require_permission("sales.create")
        def do_it(self):
            return "ok"

    with pytest.raises(SessionExpiredError):
        Protected(sessions).do_it()
