"""SqlOrganizationRepository against a live PostgreSQL database — proves
get_by_id/update round-trip real rows and that a partial update only
touches the fields it's given.

Uses the ``live_db`` fixture (tests/conftest.py) — see its docstring for
why this is gated on INVENTORY_TEST_DATABASE_URL, separate from the app's
real INVENTORY_DATABASE_URL, and how to run these locally.
"""
from decimal import Decimal

from app.database.session import get_session
from app.domain.backup import BackupFrequency
from app.domain.inventory import LowStockBehavior, StockValuationMethod
from app.models import Organization, Warehouse
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


def test_new_organization_gets_every_settings_section_default(live_db):
    org_id = _make_org(live_db)
    result = _repo().get_by_id(org_id)

    assert result.allow_negative_stock is False
    assert result.default_warehouse_id is None
    assert result.low_stock_behavior == LowStockBehavior.WARN_ONLY
    assert result.stock_valuation_method == StockValuationMethod.WEIGHTED_AVERAGE
    assert result.default_tax_percent == Decimal("0")
    assert result.default_discount_percent == Decimal("0")
    assert result.purchase_number_prefix == "PO-"
    assert result.session_timeout_minutes == 30
    assert result.password_min_length == 8
    assert result.password_require_uppercase is False
    assert result.backup_directory is None
    assert result.backup_frequency == BackupFrequency.MANUAL
    assert result.backup_retention_count is None
    assert result.has_logo is False


def test_update_round_trips_every_new_settings_section(live_db):
    org_id = _make_org(live_db)
    with get_session() as session:
        warehouse = Warehouse(organization_id=org_id, code="MAIN", name="Main")
        session.add(warehouse)
        session.flush()
        warehouse_id = warehouse.id

    updated = _repo().update(org_id, OrganizationUpdate(
        allow_negative_stock=True, default_warehouse_id=warehouse_id,
        low_stock_behavior=LowStockBehavior.BLOCK_SALE,
        stock_valuation_method=StockValuationMethod.FIFO,
        default_tax_percent=Decimal("18"), default_discount_percent=Decimal("5"),
        purchase_number_prefix="PUR-", session_timeout_minutes=15,
        password_min_length=12, password_require_uppercase=True,
        password_require_number=True, password_require_special_char=True,
        backup_directory="/srv/backups", backup_frequency=BackupFrequency.DAILY,
        backup_retention_count=7))

    assert updated.allow_negative_stock is True
    assert updated.default_warehouse_id == warehouse_id
    assert updated.low_stock_behavior == LowStockBehavior.BLOCK_SALE
    assert updated.stock_valuation_method == StockValuationMethod.FIFO
    assert updated.default_tax_percent == Decimal("18.00")
    assert updated.default_discount_percent == Decimal("5.00")
    assert updated.purchase_number_prefix == "PUR-"
    assert updated.session_timeout_minutes == 15
    assert updated.password_min_length == 12
    assert updated.password_require_uppercase is True
    assert updated.password_require_number is True
    assert updated.password_require_special_char is True
    assert updated.backup_directory == "/srv/backups"
    assert updated.backup_frequency == BackupFrequency.DAILY
    assert updated.backup_retention_count == 7


def test_logo_round_trips_via_dedicated_get_logo(live_db):
    org_id = _make_org(live_db)
    repo = _repo()
    assert repo.get_logo(org_id) is None

    updated = repo.update(org_id, OrganizationUpdate(
        logo_image=b"\x89PNG\r\n", logo_content_type="image/png"))
    assert updated.has_logo is True
    assert updated.logo_content_type == "image/png"

    fetched = repo.get_by_id(org_id)
    assert fetched.has_logo is True  # bytes excluded from OrganizationOut itself

    logo = repo.get_logo(org_id)
    assert logo == (b"\x89PNG\r\n", "image/png")
