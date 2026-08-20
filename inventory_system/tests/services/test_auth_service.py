"""AuthService tested against a hand-written fake UserRepository — no
database. Proves login/logout/change-password and session-timeout
interplay without needing Postgres.
"""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.core.exceptions import (
    AccountLockedError,
    AmbiguousOrganizationError,
    InvalidCredentialsError,
    UserNotFoundError,
)
from app.domain.backup import BackupFrequency
from app.domain.inventory import LowStockBehavior, StockValuationMethod
from app.schemas.organization import OrganizationOut
from app.schemas.user import MembershipOut, UserCredentials, UserOut
from app.security.authorization import require_permission
from app.security.passwords import hash_password
from app.security.session import NotAuthenticatedError, SessionExpiredError, SessionManager
from app.services.auth_service import AuthService, DEFAULT_LOCKOUT_THRESHOLD

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
        self.profiles: dict[uuid.UUID, dict] = {}

    def seed_user(self, email, password, *, is_active=True, is_superuser=False,
                 must_change_password=False, memberships=(), full_name="Test User",
                 username="testuser"):
        user_id = uuid.uuid4()
        self.users[user_id] = UserCredentials(
            id=user_id, email=email, hashed_password=hash_password(password),
            is_active=is_active, is_superuser=is_superuser,
            must_change_password=must_change_password)
        self.memberships[user_id] = list(memberships)
        self.profiles[user_id] = {"full_name": full_name, "username": username, "phone": None,
                                  "created_at": datetime.now(timezone.utc)}
        return user_id

    def set_active(self, user_id, is_active):
        old = self.users[user_id]
        self.users[user_id] = old.model_copy(update={"is_active": is_active})
        return True

    # --- UserRepository protocol -----------------------------------
    def get_by_id(self, user_id):
        credentials = self.users.get(user_id)
        profile = self.profiles.get(user_id)
        if credentials is None or profile is None:
            return None
        return UserOut(id=user_id, email=credentials.email, username=profile["username"],
                      full_name=profile["full_name"], phone=profile["phone"],
                      is_active=credentials.is_active, is_superuser=credentials.is_superuser,
                      must_change_password=credentials.must_change_password,
                      created_at=profile["created_at"], last_login_at=None)

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


def _org(org_id, name) -> OrganizationOut:
    now = datetime.now(timezone.utc)
    return OrganizationOut(
        id=org_id, name=name, legal_name=None, tax_id=None, address=None, phone=None,
        email=None, website=None, is_active=True, logo_content_type=None, has_logo=False,
        allow_negative_stock=False, default_warehouse_id=None,
        low_stock_behavior=LowStockBehavior.WARN_ONLY,
        stock_valuation_method=StockValuationMethod.WEIGHTED_AVERAGE,
        invoice_number_prefix="INV-", default_tax_percent=Decimal("0"),
        default_discount_percent=Decimal("0"), purchase_number_prefix="PO-",
        session_timeout_minutes=30, password_min_length=8,
        password_require_uppercase=False, password_require_number=False,
        password_require_special_char=False, backup_directory=None,
        backup_frequency=BackupFrequency.MANUAL, backup_retention_count=None,
        created_at=now, updated_at=now)


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


def _service(repo=None, sessions=None, audit_log=None, organizations=None, **lockout_kwargs):
    repo = repo or FakeUserRepository()
    sessions = sessions or SessionManager(idle_timeout=timedelta(minutes=30))
    audit_log = audit_log or FakeAuditLogRepository()
    organizations = organizations or FakeOrganizationRepository()
    return (AuthService(repo, sessions, audit_log, organizations, **lockout_kwargs),
           repo, sessions, audit_log)


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


def test_login_ambiguous_organization_error_carries_candidate_organizations():
    """Regression test: AmbiguousOrganizationError used to carry no data at
    all, so LoginWindow had no way to render an organization picker and a
    multi-org account with no default was permanently stuck on the login
    screen (see app.ui.login_window). AuthService.login must now populate
    the exception with (organization_id, organization_name) pairs the UI
    can build a picker from directly.
    """
    repo = FakeUserRepository()
    user_id = repo.seed_user("owner@acme.test", "s3cret!")
    repo.memberships[user_id] = [
        _membership(user_id, org_id=ORG_ID, is_default=False),
        _membership(user_id, org_id=OTHER_ORG_ID, is_default=False),
    ]
    organizations = FakeOrganizationRepository()
    organizations.orgs[ORG_ID] = _org(ORG_ID, "Acme Retail")
    organizations.orgs[OTHER_ORG_ID] = _org(OTHER_ORG_ID, "Acme Wholesale")
    service, _, _, _ = _service(repo, organizations=organizations)

    with pytest.raises(AmbiguousOrganizationError) as exc_info:
        service.login("owner@acme.test", "s3cret!")

    candidates = dict(exc_info.value.organizations)
    assert candidates == {ORG_ID: "Acme Retail", OTHER_ORG_ID: "Acme Wholesale"}


def test_login_with_explicit_organization_id_resolves_the_ambiguity():
    repo = FakeUserRepository()
    user_id = repo.seed_user("owner@acme.test", "s3cret!")
    repo.memberships[user_id] = [
        _membership(user_id, org_id=ORG_ID, is_default=False),
        _membership(user_id, org_id=OTHER_ORG_ID, is_default=False),
    ]
    service, _, sessions, _ = _service(repo)
    session = service.login("owner@acme.test", "s3cret!", organization_id=OTHER_ORG_ID)
    assert session.organization_id == OTHER_ORG_ID
    assert sessions.is_authenticated is True


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


def test_change_password_records_an_audit_entry_without_the_password_itself():
    repo = FakeUserRepository()
    user_id = repo.seed_user("owner@acme.test", "old-pass")
    service, repo, sessions, audit_log = _service(repo)
    service.login("owner@acme.test", "old-pass")
    audit_log.entries.clear()  # drop the login-succeeded entry, isolate this call

    service.change_password("old-pass", "new-pass")

    entries = [e for e in audit_log.entries if e["action"] == "auth.password_changed"]
    assert len(entries) == 1
    assert entries[0]["user_id"] == user_id
    # Never anything password-shaped in the audit trail — not the new
    # password, not the old one, not either hash.
    changes = entries[0]["changes"]
    serialized = str(changes)
    assert "new-pass" not in serialized
    assert "old-pass" not in serialized
    assert "$argon2id$" not in serialized


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


# -- failed-login lockout ----------------------------------------------#

def test_lockout_parameters_are_configurable_not_hardcoded():
    # Proves the threshold/window/duration are real constructor
    # parameters (wired from Settings by Container.auth_service in
    # production), not module-level constants a caller can't override —
    # a threshold of 2 must lock out after the 2nd failure, not the
    # module's own default of DEFAULT_LOCKOUT_THRESHOLD (5).
    repo = FakeUserRepository()
    repo.seed_user("owner@acme.test", "s3cret!")
    service, _, _, _ = _service(repo, lockout_threshold=2,
                                lockout_window=timedelta(minutes=1),
                                lockout_duration=timedelta(minutes=1))

    with pytest.raises(InvalidCredentialsError):
        service.login("owner@acme.test", "wrong")
    with pytest.raises(InvalidCredentialsError):
        service.login("owner@acme.test", "wrong")

    with pytest.raises(AccountLockedError):
        service.login("owner@acme.test", "s3cret!")


def test_default_lockout_threshold_matches_documented_default():
    assert DEFAULT_LOCKOUT_THRESHOLD == 5


def test_repeated_wrong_password_locks_out_further_attempts():
    repo = FakeUserRepository()
    repo.seed_user("owner@acme.test", "s3cret!")
    service, _, sessions, _ = _service(repo)

    for _ in range(5):
        with pytest.raises(InvalidCredentialsError):
            service.login("owner@acme.test", "wrong")

    with pytest.raises(AccountLockedError):
        service.login("owner@acme.test", "wrong")
    # Even the *correct* password is rejected while locked out.
    with pytest.raises(AccountLockedError):
        service.login("owner@acme.test", "s3cret!")
    assert sessions.is_authenticated is False


def test_lockout_is_scoped_to_a_single_email():
    repo = FakeUserRepository()
    repo.seed_user("owner@acme.test", "s3cret!")
    repo.seed_user("other@acme.test", "different!")
    service, _, sessions, _ = _service(repo)

    for _ in range(5):
        with pytest.raises(InvalidCredentialsError):
            service.login("owner@acme.test", "wrong")

    # A different email is unaffected.
    session = service.login("other@acme.test", "different!")
    assert session is not None


def test_lockout_tracking_is_case_and_whitespace_insensitive():
    repo = FakeUserRepository()
    repo.seed_user("owner@acme.test", "s3cret!")
    service, _, _, _ = _service(repo)

    for _ in range(5):
        with pytest.raises(InvalidCredentialsError):
            service.login("  Owner@Acme.TEST  ", "wrong")

    with pytest.raises(AccountLockedError):
        service.login("owner@acme.test", "s3cret!")


def test_successful_login_clears_the_failure_count():
    repo = FakeUserRepository()
    repo.seed_user("owner@acme.test", "s3cret!")
    service, _, sessions, _ = _service(repo)

    for _ in range(4):  # one under the threshold
        with pytest.raises(InvalidCredentialsError):
            service.login("owner@acme.test", "wrong")

    service.login("owner@acme.test", "s3cret!")
    sessions.end()

    # The slate was wiped by the successful login — one more wrong guess
    # doesn't immediately lock out (it would if the old count had carried
    # over from 4 -> 5).
    with pytest.raises(InvalidCredentialsError):
        service.login("owner@acme.test", "wrong")
    session = service.login("owner@acme.test", "s3cret!")
    assert session is not None


def test_lockout_also_applies_to_an_unknown_email():
    service, _, _, _ = _service()
    for _ in range(5):
        with pytest.raises(InvalidCredentialsError):
            service.login("nobody@acme.test", "whatever")
    with pytest.raises(AccountLockedError):
        service.login("nobody@acme.test", "whatever")


# -- current user / live active-status ----------------------------------#

def test_get_current_user_returns_the_logged_in_users_full_profile():
    repo = FakeUserRepository()
    user_id = repo.seed_user("owner@acme.test", "s3cret!", full_name="Owner Person",
                             username="owner")
    service, _, _, _ = _service(repo)
    service.login("owner@acme.test", "s3cret!")

    current = service.get_current_user()
    assert current.id == user_id
    assert current.full_name == "Owner Person"
    assert current.username == "owner"


def test_get_current_user_requires_a_session():
    service, _, _, _ = _service()
    with pytest.raises(NotAuthenticatedError):
        service.get_current_user()


def test_get_current_user_raises_if_the_user_row_is_gone():
    repo = FakeUserRepository()
    user_id = repo.seed_user("owner@acme.test", "s3cret!")
    service, _, _, _ = _service(repo)
    service.login("owner@acme.test", "s3cret!")

    del repo.profiles[user_id]  # simulate the row vanishing after login

    with pytest.raises(UserNotFoundError):
        service.get_current_user()


def test_get_current_membership_returns_role_for_the_sessions_organization():
    repo = FakeUserRepository()
    user_id = repo.seed_user("owner@acme.test", "s3cret!")
    repo.memberships[user_id] = [_membership(user_id)]
    service, _, _, _ = _service(repo)
    service.login("owner@acme.test", "s3cret!")

    membership = service.get_current_membership()
    assert membership is not None
    assert membership.organization_id == ORG_ID
    assert membership.role_name == "MANAGER"


def test_get_current_membership_is_none_for_a_superuser_with_no_org():
    repo = FakeUserRepository()
    repo.seed_user("root@acme.test", "s3cret!", is_superuser=True)
    service, _, _, _ = _service(repo)
    service.login("root@acme.test", "s3cret!")

    assert service.get_current_membership() is None


def test_is_current_user_still_active_true_while_active():
    repo = FakeUserRepository()
    repo.seed_user("owner@acme.test", "s3cret!")
    service, _, _, _ = _service(repo)
    service.login("owner@acme.test", "s3cret!")

    assert service.is_current_user_still_active() is True


def test_is_current_user_still_active_false_after_admin_deactivates_them():
    repo = FakeUserRepository()
    user_id = repo.seed_user("owner@acme.test", "s3cret!")
    service, _, _, _ = _service(repo)
    service.login("owner@acme.test", "s3cret!")

    # Simulate a different admin deactivating this account mid-session —
    # the live session doesn't know yet (it only has a permission snapshot
    # from login time), but a poll against the repository does.
    repo.set_active(user_id, False)

    assert service.is_current_user_still_active() is False


def test_is_current_user_still_active_true_when_nobody_is_logged_in():
    service, _, _, _ = _service()
    assert service.is_current_user_still_active() is True


# -- audit logging --------------------------------------------------------- #
# Beyond "was an entry recorded": every one of these also asserts the
# password involved in the attempt never appears anywhere in the audit
# trail, per the hard requirement that audit metadata must never carry a
# credential — the same rule test_change_password_records_an_audit_entry_
# without_the_password_itself already proves for change_password.

def test_login_success_records_audit_entry_without_the_password():
    repo = FakeUserRepository()
    user_id = repo.seed_user("owner@acme.test", "s3cret!")
    repo.memberships[user_id] = [_membership(user_id)]
    service, repo, sessions, audit_log = _service(repo)

    service.login("owner@acme.test", "s3cret!")

    entries = [e for e in audit_log.entries if e["action"] == "auth.login_succeeded"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["user_id"] == user_id
    assert entry["entity_type"] == "user"
    assert entry["entity_id"] == user_id
    assert entry["actor_email"] == "owner@acme.test"
    assert "s3cret!" not in str(audit_log.entries)


def test_login_failure_records_audit_entry_without_the_attempted_password():
    repo = FakeUserRepository()
    user_id = repo.seed_user("owner@acme.test", "s3cret!")
    service, repo, sessions, audit_log = _service(repo)

    with pytest.raises(InvalidCredentialsError):
        service.login("owner@acme.test", "totally-wrong-password")

    entries = [e for e in audit_log.entries if e["action"] == "auth.login_failed"]
    assert len(entries) == 1
    assert entries[0]["user_id"] == user_id
    assert entries[0]["changes"] == {"reason": "wrong_password"}
    assert "totally-wrong-password" not in str(audit_log.entries)


def test_login_failure_for_unknown_email_records_audit_entry():
    service, _, _, audit_log = _service()

    with pytest.raises(InvalidCredentialsError):
        service.login("nobody@acme.test", "whatever-password")

    entries = [e for e in audit_log.entries if e["action"] == "auth.login_failed"]
    assert len(entries) == 1
    assert entries[0]["user_id"] is None
    assert entries[0]["changes"] == {"reason": "unknown_email"}
    assert "whatever-password" not in str(audit_log.entries)


def test_logout_records_audit_entry():
    repo = FakeUserRepository()
    user_id = repo.seed_user("owner@acme.test", "s3cret!")
    service, repo, sessions, audit_log = _service(repo)
    service.login("owner@acme.test", "s3cret!")
    audit_log.entries.clear()  # isolate from the login entry

    service.logout()

    entries = [e for e in audit_log.entries if e["action"] == "auth.logout"]
    assert len(entries) == 1
    assert entries[0]["user_id"] == user_id
