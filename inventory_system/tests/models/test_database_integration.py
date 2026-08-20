"""Integration tests against a real PostgreSQL database — proves the ORM
layer (transactions, FK/check-constraint enforcement, relationship
loading), not just that the models import cleanly. UUID/JSONB/INET are
Postgres-specific types, so these intentionally do not run against SQLite
or the excel backend.

Uses the ``live_db`` fixture (tests/conftest.py) — skipped automatically
unless INVENTORY_TEST_DATABASE_URL is set to a scratch database. See that
file's docstring for why this is gated on a separate setting from
INVENTORY_DATABASE_URL, and for how to run these locally.
"""
import pytest
from sqlalchemy.exc import IntegrityError

from app.database.session import get_session
from app.models import Organization, Permission, Role, RolePermission, User, UserOrganization
from app.security.passwords import hash_password, verify_password


def _make_owner_membership(session):
    org = Organization(name="Acme Traders", tax_id="123-456")
    role = Role(name="Owner", is_system=True)
    perm = Permission(code="bills.create")
    role.permissions.append(RolePermission(permission=perm))
    user = User(email="owner@acme.test", username="owner",
               hashed_password=hash_password("s3cret!"), full_name="Ada Owner")
    session.add_all([org, role, perm, user])
    session.flush()
    session.add(UserOrganization(user_id=user.id, organization_id=org.id, role_id=role.id,
                                 is_default=True))
    return org, role, user


def test_transactional_insert_commits(live_db):
    with get_session() as session:
        _make_owner_membership(session)
    with get_session() as session:
        assert session.query(User).filter_by(email="owner@acme.test").count() == 1


def test_relationships_load_and_password_verifies(live_db):
    with get_session() as session:
        _make_owner_membership(session)
    with get_session() as session:
        fetched = session.query(User).filter_by(email="owner@acme.test").one()
        assert len(fetched.memberships) == 1
        assert fetched.memberships[0].organization.name == "Acme Traders"
        assert fetched.memberships[0].role.name == "Owner"
        assert verify_password("s3cret!", fetched.hashed_password)
        assert not verify_password("wrong", fetched.hashed_password)


def test_duplicate_email_rejected_by_unique_index(live_db):
    with get_session() as session:
        _make_owner_membership(session)
    with pytest.raises(IntegrityError):
        with get_session() as session:
            session.add(User(email="owner@acme.test", username="dup",
                             hashed_password=hash_password("x"), full_name="Dup"))


def test_mixed_case_email_rejected_by_check_constraint(live_db):
    with pytest.raises(IntegrityError):
        with get_session() as session:
            session.add(User(email="Mixed@Case.test", username="baduser",
                             hashed_password=hash_password("x"), full_name="Bad"))


def test_deleting_assigned_role_is_restricted(live_db):
    with get_session() as session:
        _org, role, _user = _make_owner_membership(session)
        role_id = role.id
    with pytest.raises(IntegrityError):
        with get_session() as session:
            session.delete(session.get(Role, role_id))


def test_deleting_organization_with_financial_history_is_restricted(live_db):
    """A DELETE FROM organizations run outside the app (a support script, a
    bad migration — there is no delete_organization feature anywhere in
    the app itself) must never silently cascade away an organization's
    invoices/payments/inventory ledger. Every business/financial table's
    organization_id FK was CASCADE until this was fixed; this is the
    regression test for that fix — see migration b7d4e91a5c33.
    """
    from app.models import Customer

    with get_session() as session:
        org = Organization(name="Has Financial History")
        session.add(org)
        session.flush()
        session.add(Customer(organization_id=org.id, name="Some Customer"))
        org_id = org.id

    with pytest.raises(IntegrityError):
        with get_session() as session:
            session.delete(session.get(Organization, org_id))

    # Unaffected once the offending child row is gone first — proves this
    # is RESTRICT (blocks until cleared), not a permanently broken delete.
    with get_session() as session:
        session.query(Customer).filter_by(organization_id=org_id).delete()
    with get_session() as session:
        session.delete(session.get(Organization, org_id))
    with get_session() as session:
        assert session.get(Organization, org_id) is None


def test_rollback_on_exception_persists_nothing(live_db):
    try:
        with get_session() as session:
            session.add(Organization(name="Should Not Persist"))
            raise RuntimeError("force rollback")
    except RuntimeError:
        pass
    with get_session() as session:
        assert session.query(Organization).filter_by(
            name="Should Not Persist").first() is None
