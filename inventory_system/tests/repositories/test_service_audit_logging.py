"""ProductService and OrganizationService, wired to real SQL repositories,
against a live PostgreSQL database — proves the two remaining
service-layer (best-effort, non-atomic) audit categories that have no
existing live-DB coverage: product changes and settings changes.

Login/logout/user-creation/permission-change audit entries are covered in
tests/repositories/test_sql_user_repository.py; inventory/purchase/sales
audit entries (written atomically inside the repository's own
transaction) are covered in tests/repositories/test_sql_inventory_repository.py,
test_sql_purchase_repository.py, and test_sql_sales_repository.py.

Uses the ``live_db`` fixture (tests/conftest.py) — see its docstring for
why this is gated on INVENTORY_TEST_DATABASE_URL, separate from the app's
real INVENTORY_DATABASE_URL, and how to run these locally.
"""
from datetime import timedelta
from decimal import Decimal

import pytest

from app.database.session import get_session
from app.models import AuditLog, Organization, Unit, User
from app.repositories.sql.audit_log_repository import SqlAuditLogRepository
from app.repositories.sql.organization_repository import SqlOrganizationRepository
from app.repositories.sql.warehouse_repository import SqlWarehouseRepository
from app.repositories.sql.product_repository import SqlProductRepository
from app.schemas.organization import OrganizationUpdate
from app.schemas.product import ProductCreate, ProductUpdate
from app.security.session import SessionManager
from app.services.organization_service import OrganizationService
from app.services.product_service import ProductService


@pytest.fixture()
def world(live_db):
    with get_session() as session:
        org = Organization(name="Acme Traders")
        session.add(org)
        session.flush()
        unit = Unit(organization_id=org.id, name="Piece", abbreviation="pc")
        admin = User(email="admin@acme.test", username="admin", hashed_password="x",
                    full_name="Admin")
        session.add_all([unit, admin])
        session.flush()
        return {"org_id": org.id, "unit_id": unit.id, "admin_id": admin.id}


def _sessions(org_id, user_id, permissions) -> SessionManager:
    import uuid
    from datetime import datetime, timezone

    sessions = SessionManager(idle_timeout=timedelta(minutes=30))
    sessions.start(user_id=user_id, organization_id=org_id, role_id=uuid.uuid4(),
                   permissions=frozenset(permissions), is_superuser=False,
                   must_change_password=False, now=datetime.now(timezone.utc))
    return sessions


def _product_data(**overrides):
    kwargs = dict(sku="SKU-1", barcode=None, name="Widget", unit_id=None,
                  purchase_price=Decimal("10"), selling_price=Decimal("15"),
                  tax_percent=Decimal("13"), minimum_stock_level=Decimal("0"))
    kwargs.update(overrides)
    return ProductCreate(**kwargs)


# -- product changes --------------------------------------------------------#

def test_create_product_records_audit_log_entry(world):
    sessions = _sessions(world["org_id"], world["admin_id"], {"products.create"})
    actor_id = sessions.peek().user_id
    service = ProductService(SqlProductRepository(), sessions, SqlAuditLogRepository())

    created = service.create_product(_product_data(unit_id=world["unit_id"]))

    with get_session() as session:
        entries = (session.query(AuditLog)
                  .filter_by(action="product.create", entity_id=created.id).all())
        assert len(entries) == 1
        assert entries[0].user_id == actor_id
        assert entries[0].organization_id == world["org_id"]
        assert entries[0].changes["sku"] == "SKU-1"


def test_update_product_records_audit_log_entry_with_before_after(world):
    sessions = _sessions(world["org_id"], world["admin_id"], {"products.create", "products.update"})
    service = ProductService(SqlProductRepository(), sessions, SqlAuditLogRepository())
    created = service.create_product(_product_data(unit_id=world["unit_id"]))

    service.update_product(created.id, ProductUpdate(selling_price=Decimal("20")))

    with get_session() as session:
        entries = (session.query(AuditLog)
                  .filter_by(action="product.update", entity_id=created.id).all())
        assert len(entries) == 1
        assert entries[0].changes["before"]["selling_price"] == "15.00"
        assert entries[0].changes["after"]["selling_price"] == "20"


def test_update_product_with_no_actual_changes_does_not_record_audit_log(world):
    sessions = _sessions(world["org_id"], world["admin_id"], {"products.create", "products.update"})
    service = ProductService(SqlProductRepository(), sessions, SqlAuditLogRepository())
    created = service.create_product(_product_data(unit_id=world["unit_id"]))

    service.update_product(created.id, ProductUpdate(selling_price=Decimal("15")))  # unchanged

    with get_session() as session:
        entries = (session.query(AuditLog)
                  .filter_by(action="product.update", entity_id=created.id).all())
        assert len(entries) == 0


def test_archive_and_restore_product_record_audit_log_entries(world):
    sessions = _sessions(world["org_id"], world["admin_id"], {"products.create", "products.delete", "products.update"})
    service = ProductService(SqlProductRepository(), sessions, SqlAuditLogRepository())
    created = service.create_product(_product_data(unit_id=world["unit_id"]))

    service.archive_product(created.id)
    service.restore_product(created.id)

    with get_session() as session:
        archive_entries = (session.query(AuditLog)
                          .filter_by(action="product.archive", entity_id=created.id).all())
        restore_entries = (session.query(AuditLog)
                          .filter_by(action="product.restore", entity_id=created.id).all())
        assert len(archive_entries) == 1
        assert len(restore_entries) == 1


# -- settings changes --------------------------------------------------------#

def test_update_organization_records_audit_log_entry_with_before_after(world):
    sessions = _sessions(world["org_id"], world["admin_id"], {"settings.manage"})
    actor_id = sessions.peek().user_id
    service = OrganizationService(SqlOrganizationRepository(), sessions, SqlAuditLogRepository(),
                                    SqlWarehouseRepository())

    service.update_organization(OrganizationUpdate(phone="+1-555-0100",
                                                    email="hi@acme.example"))

    with get_session() as session:
        entries = (session.query(AuditLog)
                  .filter_by(action="settings.update", entity_id=world["org_id"]).all())
        assert len(entries) == 1
        assert entries[0].user_id == actor_id
        assert entries[0].changes["after"]["phone"] == "+1-555-0100"
        assert entries[0].changes["after"]["email"] == "hi@acme.example"


def test_update_organization_with_no_actual_changes_does_not_record_audit_log(world):
    sessions = _sessions(world["org_id"], world["admin_id"], {"settings.manage"})
    service = OrganizationService(SqlOrganizationRepository(), sessions, SqlAuditLogRepository(),
                                    SqlWarehouseRepository())

    service.update_organization(OrganizationUpdate(name="Acme Traders"))  # already this name

    with get_session() as session:
        entries = (session.query(AuditLog)
                  .filter_by(action="settings.update", entity_id=world["org_id"]).all())
        assert len(entries) == 0
