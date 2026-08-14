"""SqlOrganizationRepository against a live PostgreSQL database — proves
get_by_id/update round-trip real rows and that a partial update only
touches the fields it's given.

Uses the ``live_db`` fixture (tests/conftest.py) — see its docstring for
why this is gated on INVENTORY_TEST_DATABASE_URL, separate from the app's
real INVENTORY_DATABASE_URL, and how to run these locally.
"""
from app.database.session import get_session
from app.models import Organization
from app.repositories.sql.organization_repository import SqlOrganizationRepository
from app.schemas.organization import OrganizationUpdate


def _repo():
    return SqlOrganizationRepository()


def _make_org(live_db, **overrides):
    with get_session() as session:
        org = Organization(name="Acme Co", **overrides)
        session.add(org)
        session.flush()
        return org.id


def test_get_by_id_returns_company_fields(live_db):
    org_id = _make_org(live_db, legal_name="Acme Co Pvt. Ltd.", tax_id="PAN-1",
                       address="123 Main St", phone="+1-555-0100", email="hi@acme.example",
                       website="acme.example")
    result = _repo().get_by_id(org_id)

    assert result is not None
    assert result.name == "Acme Co"
    assert result.legal_name == "Acme Co Pvt. Ltd."
    assert result.tax_id == "PAN-1"
    assert result.address == "123 Main St"
    assert result.phone == "+1-555-0100"
    assert result.email == "hi@acme.example"
    assert result.website == "acme.example"
    assert result.invoice_number_prefix == "INV-"   # model default


def test_get_by_id_missing_returns_none(live_db):
    import uuid
    assert _repo().get_by_id(uuid.uuid4()) is None


def test_update_only_touches_fields_that_were_set(live_db):
    org_id = _make_org(live_db, phone="+1-555-0100", email="old@acme.example")
    repo = _repo()

    updated = repo.update(org_id, OrganizationUpdate(email="new@acme.example"))

    assert updated.email == "new@acme.example"
    assert updated.phone == "+1-555-0100"   # untouched


def test_update_missing_organization_returns_none(live_db):
    import uuid
    result = _repo().update(uuid.uuid4(), OrganizationUpdate(name="Doesn't matter"))
    assert result is None


def test_update_invoice_number_prefix(live_db):
    org_id = _make_org(live_db)
    updated = _repo().update(org_id, OrganizationUpdate(invoice_number_prefix="ACME-"))
    assert updated.invoice_number_prefix == "ACME-"
