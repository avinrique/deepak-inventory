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
from app.models import Organization, Permission, Role, RolePermission
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
    need to call UserService.create_user (which requires users.manage)."""
    sessions = SessionManager(idle_timeout=timedelta(minutes=30))
    sessions.start(user_id=uuid.uuid4(), organization_id=org_id, role_id=role_id,
                   permissions=frozenset({"users.manage"}), is_superuser=False,
                   must_change_password=False, now=datetime.now(timezone.utc))
    return sessions


def test_create_user_then_login_end_to_end(org_and_role):
    org_id, role_id = org_and_role
    users = SqlUserRepository()
    admin_sessions = _admin_session_manager(org_id, role_id)
    user_service = UserService(users, admin_sessions)

    created = user_service.create_user("staff@acme.test", "Staff Person", "s3cret!",
                                       org_id, role_id)
    assert created.email == "staff@acme.test"

    login_sessions = SessionManager(idle_timeout=timedelta(minutes=30))
    session = AuthService(users, login_sessions).login("staff@acme.test", "s3cret!")
    assert session.organization_id == org_id
    assert session.permissions == frozenset({"sales.create"})


def test_login_rejects_wrong_password_against_real_db(org_and_role):
    org_id, role_id = org_and_role
    users = SqlUserRepository()
    admin_sessions = _admin_session_manager(org_id, role_id)
    UserService(users, admin_sessions).create_user(
        "staff@acme.test", "Staff Person", "s3cret!", org_id, role_id)

    login_sessions = SessionManager(idle_timeout=timedelta(minutes=30))
    with pytest.raises(InvalidCredentialsError):
        AuthService(users, login_sessions).login("staff@acme.test", "wrong-password")


def test_deactivated_user_cannot_log_in(org_and_role):
    org_id, role_id = org_and_role
    users = SqlUserRepository()
    admin_sessions = _admin_session_manager(org_id, role_id)
    user_service = UserService(users, admin_sessions)
    created = user_service.create_user("staff@acme.test", "Staff Person", "s3cret!",
                                       org_id, role_id)
    user_service.deactivate_user(created.id)

    login_sessions = SessionManager(idle_timeout=timedelta(minutes=30))
    with pytest.raises(InvalidCredentialsError):
        AuthService(users, login_sessions).login("staff@acme.test", "s3cret!")


def test_admin_reset_password_forces_change_on_next_login(org_and_role):
    org_id, role_id = org_and_role
    users = SqlUserRepository()
    admin_sessions = _admin_session_manager(org_id, role_id)
    user_service = UserService(users, admin_sessions)
    created = user_service.create_user("staff@acme.test", "Staff Person", "s3cret!",
                                       org_id, role_id)
    temp_password = user_service.reset_password(created.id)

    login_sessions = SessionManager(idle_timeout=timedelta(minutes=30))
    session = AuthService(users, login_sessions).login("staff@acme.test", temp_password)
    assert session.must_change_password is True
