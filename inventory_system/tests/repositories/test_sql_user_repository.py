"""SqlUserRepository (and AuthService/UserService wired to the *real*
repository, not fakes) against a live PostgreSQL database — proves the
schemas/method signatures the fake-repository service tests assume
actually match what SQLAlchemy returns.

Uses the ``live_db`` fixture (tests/conftest.py) — skipped automatically
unless INVENTORY_TEST_DATABASE_URL is set to a scratch database. See that
file's docstring for why this is gated on a separate setting from
INVENTORY_DATABASE_URL, and for how to run these locally.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.core.exceptions import InvalidCredentialsError
from app.database.session import get_session
from app.models import AuditLog, Organization, Permission, Role, RolePermission, User
from app.repositories.sql.audit_log_repository import SqlAuditLogRepository
from app.repositories.sql.organization_repository import SqlOrganizationRepository
from app.repositories.sql.user_repository import SqlUserRepository
from app.security.session import SessionManager
from app.services.auth_service import AuthService
from app.services.user_service import UserService


@pytest.fixture()
def org_and_role(live_db):
    with get_session() as session:
        org = Organization(name="Acme Traders")
        role = Role(name="MANAGER", is_system=True)
        perm = Permission(code="sales.create")
        role.permissions.append(RolePermission(permission=perm))
        session.add_all([org, role, perm])
        session.flush()
        org_id, role_id = org.id, role.id
    return org_id, role_id


def _admin_session_manager(org_id, role_id) -> SessionManager:
    """A SessionManager pre-loaded with an admin session, for tests that
    need to call UserService.create_user (which requires users.manage). The
    session's user_id is a real User row — audit_logs.user_id has a real
    foreign key to users.id, so a synthetic uuid4() here (the previous
    approach) fails once the action being tested writes an audit entry.
    """
    with get_session() as session:
        suffix = uuid.uuid4().hex[:8]
        admin = User(email=f"admin-{suffix}@acme.test", username=f"admin-{suffix}",
                    hashed_password="x", full_name="Admin")
        session.add(admin)
        session.flush()
        admin_id = admin.id
    sessions = SessionManager(idle_timeout=timedelta(minutes=30))
    sessions.start(user_id=admin_id, organization_id=org_id, role_id=role_id,
                   permissions=frozenset({"users.view", "users.create", "users.update",
                                         "users.deactivate", "users.reset_password",
                                         "users.manage_roles"}),
                   is_superuser=False, must_change_password=False,
                   now=datetime.now(timezone.utc))
    return sessions


def test_create_user_then_login_end_to_end(org_and_role):
    org_id, role_id = org_and_role
    users = SqlUserRepository()
    admin_sessions = _admin_session_manager(org_id, role_id)
    user_service = UserService(users, admin_sessions, SqlAuditLogRepository(), SqlOrganizationRepository())

    created = user_service.create_user("staff@acme.test", "Staff Person", "s3cret!1",
                                       org_id, role_id)
    assert created.email == "staff@acme.test"

    login_sessions = SessionManager(idle_timeout=timedelta(minutes=30))
    session = AuthService(users, login_sessions, SqlAuditLogRepository(), SqlOrganizationRepository()).login("staff@acme.test", "s3cret!1")
    assert session.organization_id == org_id
    assert session.permissions == frozenset({"sales.create"})


def test_login_rejects_wrong_password_against_real_db(org_and_role):
    org_id, role_id = org_and_role
    users = SqlUserRepository()
    admin_sessions = _admin_session_manager(org_id, role_id)
    UserService(users, admin_sessions, SqlAuditLogRepository(), SqlOrganizationRepository()).create_user(
        "staff@acme.test", "Staff Person", "s3cret!1", org_id, role_id)

    login_sessions = SessionManager(idle_timeout=timedelta(minutes=30))
    with pytest.raises(InvalidCredentialsError):
        AuthService(users, login_sessions, SqlAuditLogRepository(), SqlOrganizationRepository()).login("staff@acme.test", "wrong-password")


def test_deactivated_user_cannot_log_in(org_and_role):
    org_id, role_id = org_and_role
    users = SqlUserRepository()
    admin_sessions = _admin_session_manager(org_id, role_id)
    user_service = UserService(users, admin_sessions, SqlAuditLogRepository(), SqlOrganizationRepository())
    created = user_service.create_user("staff@acme.test", "Staff Person", "s3cret!1",
                                       org_id, role_id)
    user_service.deactivate_user(created.id)

    login_sessions = SessionManager(idle_timeout=timedelta(minutes=30))
    with pytest.raises(InvalidCredentialsError):
        AuthService(users, login_sessions, SqlAuditLogRepository(), SqlOrganizationRepository()).login("staff@acme.test", "s3cret!1")


def test_admin_reset_password_forces_change_on_next_login(org_and_role):
    org_id, role_id = org_and_role
    users = SqlUserRepository()
    admin_sessions = _admin_session_manager(org_id, role_id)
    user_service = UserService(users, admin_sessions, SqlAuditLogRepository(), SqlOrganizationRepository())
    created = user_service.create_user("staff@acme.test", "Staff Person", "s3cret!1",
                                       org_id, role_id)
    temp_password = user_service.reset_password(created.id)

    login_sessions = SessionManager(idle_timeout=timedelta(minutes=30))
    session = AuthService(users, login_sessions, SqlAuditLogRepository(), SqlOrganizationRepository()).login("staff@acme.test", temp_password)
    assert session.must_change_password is True


def test_get_user_returns_profile_with_role_from_a_real_join(org_and_role):
    org_id, role_id = org_and_role
    users = SqlUserRepository()
    admin_sessions = _admin_session_manager(org_id, role_id)
    user_service = UserService(users, admin_sessions, SqlAuditLogRepository(),
                               SqlOrganizationRepository())
    created = user_service.create_user("staff@acme.test", "Staff Person", "s3cret!1",
                                       org_id, role_id, username="staffer",
                                       phone="+1 555-0100")

    fetched = user_service.get_user(created.id)
    assert fetched.email == "staff@acme.test"
    assert fetched.username == "staffer"
    assert fetched.phone == "+1 555-0100"
    assert fetched.role_name == "MANAGER"


def test_update_user_persists_and_returns_new_values(org_and_role):
    org_id, role_id = org_and_role
    users = SqlUserRepository()
    admin_sessions = _admin_session_manager(org_id, role_id)
    user_service = UserService(users, admin_sessions, SqlAuditLogRepository(),
                               SqlOrganizationRepository())
    created = user_service.create_user("staff@acme.test", "Staff Person", "s3cret!1",
                                       org_id, role_id)

    from app.schemas.user import UserUpdate
    updated = user_service.update_user(created.id, UserUpdate(full_name="New Name",
                                                              phone="+1 555-0199"))
    assert updated.full_name == "New Name"
    assert updated.phone == "+1 555-0199"
    assert updated.email == "staff@acme.test"  # unset field left alone

    refetched = user_service.get_user(created.id)
    assert refetched.full_name == "New Name"


def test_update_user_rejects_duplicate_email_against_real_db(org_and_role):
    from app.core.exceptions import DuplicateEmailError
    from app.schemas.user import UserUpdate

    org_id, role_id = org_and_role
    users = SqlUserRepository()
    admin_sessions = _admin_session_manager(org_id, role_id)
    user_service = UserService(users, admin_sessions, SqlAuditLogRepository(),
                               SqlOrganizationRepository())
    user_service.create_user("first@acme.test", "First Person", "s3cret!1", org_id, role_id)
    second = user_service.create_user("second@acme.test", "Second Person", "s3cret!1",
                                      org_id, role_id)

    with pytest.raises(DuplicateEmailError):
        user_service.update_user(second.id, UserUpdate(email="first@acme.test"))


def test_create_user_rejects_unknown_role_against_real_db(org_and_role):
    import uuid as uuid_module

    from app.core.exceptions import RoleNotFoundError

    org_id, role_id = org_and_role
    users = SqlUserRepository()
    admin_sessions = _admin_session_manager(org_id, role_id)
    user_service = UserService(users, admin_sessions, SqlAuditLogRepository(),
                               SqlOrganizationRepository())

    with pytest.raises(RoleNotFoundError):
        user_service.create_user("staff@acme.test", "Staff Person", "s3cret!1", org_id,
                                 uuid_module.uuid4())


# -- audit logging --------------------------------------------------------#

def test_create_user_records_audit_log_entry(org_and_role):
    org_id, role_id = org_and_role
    users = SqlUserRepository()
    admin_sessions = _admin_session_manager(org_id, role_id)
    admin_id = admin_sessions.peek().user_id
    created = UserService(users, admin_sessions, SqlAuditLogRepository(), SqlOrganizationRepository()).create_user(
        "staff@acme.test", "Staff Person", "s3cret!1", org_id, role_id)

    with get_session() as session:
        entries = (session.query(AuditLog)
                  .filter_by(action="user.create", entity_id=created.id).all())
        assert len(entries) == 1
        assert entries[0].user_id == admin_id
        assert entries[0].organization_id == org_id


def test_login_success_records_audit_log_entry(org_and_role):
    org_id, role_id = org_and_role
    users = SqlUserRepository()
    admin_sessions = _admin_session_manager(org_id, role_id)
    UserService(users, admin_sessions, SqlAuditLogRepository(), SqlOrganizationRepository()).create_user(
        "staff@acme.test", "Staff Person", "s3cret!1", org_id, role_id)

    login_sessions = SessionManager(idle_timeout=timedelta(minutes=30))
    AuthService(users, login_sessions, SqlAuditLogRepository(), SqlOrganizationRepository()).login(
        "staff@acme.test", "s3cret!1")

    with get_session() as session:
        entries = (session.query(AuditLog)
                  .filter_by(action="auth.login_succeeded", actor_email="staff@acme.test")
                  .all())
        assert len(entries) == 1
        assert entries[0].organization_id == org_id


def test_login_failure_records_audit_log_entry_with_reason(org_and_role):
    org_id, role_id = org_and_role
    users = SqlUserRepository()
    admin_sessions = _admin_session_manager(org_id, role_id)
    UserService(users, admin_sessions, SqlAuditLogRepository(), SqlOrganizationRepository()).create_user(
        "staff@acme.test", "Staff Person", "s3cret!1", org_id, role_id)

    login_sessions = SessionManager(idle_timeout=timedelta(minutes=30))
    with pytest.raises(InvalidCredentialsError):
        AuthService(users, login_sessions, SqlAuditLogRepository(), SqlOrganizationRepository()).login(
            "staff@acme.test", "wrong-password")

    with get_session() as session:
        entries = (session.query(AuditLog)
                  .filter_by(action="auth.login_failed", actor_email="staff@acme.test")
                  .all())
        assert len(entries) == 1
        assert entries[0].changes["reason"] == "wrong_password"


def test_login_unknown_email_records_audit_log_entry(org_and_role):
    org_id, role_id = org_and_role
    users = SqlUserRepository()
    login_sessions = SessionManager(idle_timeout=timedelta(minutes=30))
    with pytest.raises(InvalidCredentialsError):
        AuthService(users, login_sessions, SqlAuditLogRepository(), SqlOrganizationRepository()).login(
            "nobody@acme.test", "whatever")

    with get_session() as session:
        entries = (session.query(AuditLog)
                  .filter_by(action="auth.login_failed", actor_email="nobody@acme.test")
                  .all())
        assert len(entries) == 1
        assert entries[0].user_id is None
        assert entries[0].changes["reason"] == "unknown_email"


def test_logout_records_audit_log_entry(org_and_role):
    org_id, role_id = org_and_role
    users = SqlUserRepository()
    admin_sessions = _admin_session_manager(org_id, role_id)
    UserService(users, admin_sessions, SqlAuditLogRepository(), SqlOrganizationRepository()).create_user(
        "staff@acme.test", "Staff Person", "s3cret!1", org_id, role_id)

    login_sessions = SessionManager(idle_timeout=timedelta(minutes=30))
    auth_service = AuthService(users, login_sessions, SqlAuditLogRepository(), SqlOrganizationRepository())
    session = auth_service.login("staff@acme.test", "s3cret!1")
    auth_service.logout()

    with get_session() as db:
        entries = (db.query(AuditLog)
                  .filter_by(action="auth.logout", user_id=session.user_id).all())
        assert len(entries) == 1


def test_change_user_role_records_audit_log_entry(org_and_role):
    org_id, role_id = org_and_role
    with get_session() as session:
        other_role = Role(name="SUPERVISOR", is_system=True)
        session.add(other_role)
        session.flush()
        other_role_id = other_role.id

    users = SqlUserRepository()
    admin_sessions = _admin_session_manager(org_id, role_id)
    admin_id = admin_sessions.peek().user_id
    user_service = UserService(users, admin_sessions, SqlAuditLogRepository(), SqlOrganizationRepository())
    created = user_service.create_user("staff@acme.test", "Staff Person", "s3cret!1",
                                       org_id, role_id)

    user_service.change_user_role(created.id, other_role_id)

    with get_session() as session:
        entries = (session.query(AuditLog)
                  .filter_by(action="user.role_changed", entity_id=created.id).all())
        assert len(entries) == 1
        assert entries[0].user_id == admin_id
        assert entries[0].changes["before"]["role_name"] == "MANAGER"
        assert entries[0].changes["after"]["role_name"] == "SUPERVISOR"


# -- password policy (Settings-backed, not hardcoded) ----------------------#

def test_create_user_rejects_password_below_configured_minimum_length(org_and_role):
    from app.core.exceptions import PasswordPolicyViolationError

    org_id, role_id = org_and_role
    with get_session() as session:
        org = session.get(Organization, org_id)
        org.password_min_length = 12

    users = SqlUserRepository()
    admin_sessions = _admin_session_manager(org_id, role_id)
    user_service = UserService(users, admin_sessions, SqlAuditLogRepository(),
                               SqlOrganizationRepository())

    with pytest.raises(PasswordPolicyViolationError):
        user_service.create_user("staff@acme.test", "Staff Person", "short1",
                                 org_id, role_id)


def test_create_user_accepts_password_meeting_a_stricter_policy(org_and_role):
    org_id, role_id = org_and_role
    with get_session() as session:
        org = session.get(Organization, org_id)
        org.password_min_length = 10
        org.password_require_uppercase = True
        org.password_require_number = True

    users = SqlUserRepository()
    admin_sessions = _admin_session_manager(org_id, role_id)
    user_service = UserService(users, admin_sessions, SqlAuditLogRepository(),
                               SqlOrganizationRepository())

    created = user_service.create_user("staff@acme.test", "Staff Person", "Str0ngPass!",
                                       org_id, role_id)
    assert created.email == "staff@acme.test"


def test_change_password_rejects_new_password_violating_policy(org_and_role):
    from app.core.exceptions import PasswordPolicyViolationError

    org_id, role_id = org_and_role
    users = SqlUserRepository()
    admin_sessions = _admin_session_manager(org_id, role_id)
    UserService(users, admin_sessions, SqlAuditLogRepository(),
               SqlOrganizationRepository()).create_user(
        "staff@acme.test", "Staff Person", "s3cret!1", org_id, role_id)

    with get_session() as session:
        org = session.get(Organization, org_id)
        org.password_require_special_char = True

    login_sessions = SessionManager(idle_timeout=timedelta(minutes=30))
    auth_service = AuthService(users, login_sessions, SqlAuditLogRepository(),
                               SqlOrganizationRepository())
    auth_service.login("staff@acme.test", "s3cret!1")

    with pytest.raises(PasswordPolicyViolationError):
        auth_service.change_password("s3cret!1", "nopunctuationhere1")


# -- session timeout (Settings-backed, applied live on login) --------------#

def test_login_applies_organizations_session_timeout_live(org_and_role):
    from app.security.session import SessionExpiredError

    org_id, role_id = org_and_role
    with get_session() as session:
        org = session.get(Organization, org_id)
        org.session_timeout_minutes = 5

    users = SqlUserRepository()
    admin_sessions = _admin_session_manager(org_id, role_id)
    UserService(users, admin_sessions, SqlAuditLogRepository(),
               SqlOrganizationRepository()).create_user(
        "staff@acme.test", "Staff Person", "s3cret!1", org_id, role_id)

    login_sessions = SessionManager(idle_timeout=timedelta(minutes=30))
    AuthService(users, login_sessions, SqlAuditLogRepository(),
               SqlOrganizationRepository()).login("staff@acme.test", "s3cret!1")

    # 10 minutes idle: still under the SessionManager's original 30-minute
    # construction default, but past the organization's configured
    # 5-minute timeout — proves login() actually pushed the org's value
    # into the live SessionManager rather than only using the constructor
    # default.
    login_sessions._session.last_activity_at -= timedelta(minutes=10)  # noqa: SLF001
    with pytest.raises(SessionExpiredError):
        login_sessions.current(now=datetime.now(timezone.utc))
