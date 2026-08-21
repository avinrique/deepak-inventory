"""End-to-end integration test: a product created through the real Add
Product flow (ProductService.create_product, with an opening quantity and
warehouse — the exact path AddProductDialog calls) must immediately be
usable everywhere else in the app: Product search/listing, Inventory, and
Sales — without a separate import step or a second, disconnected "sale
products" list.

Uses the ``live_db`` fixture (tests/conftest.py) — gated on
INVENTORY_TEST_DATABASE_URL, never the app's real INVENTORY_DATABASE_URL.
"""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.database.session import get_session
from app.domain.product import ProductType
from app.models import Organization, Unit, User, Warehouse, Customer
from app.repositories.sql.customer_repository import SqlCustomerRepository
from app.repositories.sql.inventory_repository import SqlInventoryRepository
from app.repositories.sql.product_repository import SqlProductRepository
from app.repositories.sql.sales_repository import SqlSalesOrderRepository
from app.repositories.sql.unit_repository import SqlUnitRepository
from app.repositories.sql.warehouse_repository import SqlWarehouseRepository
from app.schemas.product import ProductCreate, ProductFilter
from app.schemas.sales import SalesOrderCreate, SalesOrderItemInput
from app.security.session import SessionManager
from app.services.product_service import ProductService


@pytest.fixture()
def world(live_db):
    with get_session() as session:
        org = Organization(name="Add Product Chain Org")
        session.add(org)
        session.flush()
        unit = Unit(organization_id=org.id, name="Piece", abbreviation="pc")
        warehouse = Warehouse(organization_id=org.id, code="MAIN", name="Main")
        customer = Customer(organization_id=org.id, name="Chain Customer")
        user = User(email="add-product-chain@example.com", username="addproductchain",
                   hashed_password="x", full_name="Clerk")
        session.add_all([unit, warehouse, customer, user])
        session.flush()
        return {"org_id": org.id, "unit_id": unit.id, "warehouse_id": warehouse.id,
               "customer_id": customer.id, "user_id": user.id}


def _sessions(org_id, user_id) -> SessionManager:
    sessions = SessionManager(idle_timeout=timedelta(minutes=30))
    sessions.start(user_id=user_id, organization_id=org_id, role_id=uuid.uuid4(),
                   permissions=frozenset({"products.create", "products.view",
                                          "inventory.adjust", "inventory.view"}),
                   is_superuser=False, must_change_password=False,
                   now=datetime.now(timezone.utc))
    return sessions


def test_product_added_via_add_product_flow_is_immediately_usable_in_a_sale(world):
    org_id, user_id = world["org_id"], world["user_id"]
    sessions = _sessions(org_id, user_id)
    product_service = ProductService(SqlProductRepository(), sessions, _NoopAuditLog(),
                                     SqlWarehouseRepository(), SqlUnitRepository())

    # -- 1. Add Product: exactly what AddProductDialog does -------------#
    data = ProductCreate(sku="CHAIN-WIDGET", name="Chain Widget", unit_id=world["unit_id"],
                         product_type=ProductType.GOODS, purchase_price=Decimal("10"),
                         selling_price=Decimal("25"), tax_percent=Decimal("10"))
    product, opening_transaction = product_service.create_product(
        data, warehouse_id=world["warehouse_id"], opening_quantity=Decimal("40"))
    assert opening_transaction is not None

    # -- 2. Product search (Products page / any picker) sees it ---------#
    page = product_service.search_products(ProductFilter(search="Chain Widget"))
    assert [p.sku for p in page.items] == ["CHAIN-WIDGET"]

    # -- 3. Inventory shows the opening stock ----------------------------#
    level = SqlInventoryRepository().get_level(org_id, product.id, world["warehouse_id"])
    assert level.quantity_on_hand == Decimal("40")

    # -- 4. Sales: create + confirm + fulfill an order for this product,
    #    using only what SalesOrderRepository/SqlProductRepository (real
    #    repositories, not fakes) already expose — no special-casing
    #    needed for a product that was just created. ---------------------#
    sales_repo = SqlSalesOrderRepository()
    so = sales_repo.create(org_id, SalesOrderCreate(
        customer_id=world["customer_id"], warehouse_id=world["warehouse_id"],
        items=[SalesOrderItemInput(product_id=product.id, quantity_ordered=Decimal("15"),
                                  unit_price=Decimal("25"), tax_percent=Decimal("10"))]),
        user_id)
    sales_repo.confirm(org_id, so.id, user_id)
    fulfilled = sales_repo.fulfill_sale(org_id, so.id, user_id)
    assert fulfilled.items[0].quantity_fulfilled == Decimal("15")

    level_after_sale = SqlInventoryRepository().get_level(org_id, product.id,
                                                          world["warehouse_id"])
    assert level_after_sale.quantity_on_hand == Decimal("25"), (
        "40 opening - 15 sold must leave exactly 25 — the same inventory ledger the "
        "Add Product opening-stock transaction wrote into")


def test_product_excise_percent_survives_the_full_new_bill_finalize_chain(world):
    """A product created with a nonzero excise_percent must have that
    excise duty reflected end-to-end: product -> sales order item ->
    finalize_new_bill's confirm+fulfill+invoice, landing correctly on the
    generated Invoice's excise_amount/total_amount — proving excise
    survives the whole atomic New-Bill finalize path with the same rigor
    the discount/tax path already gets.
    """
    org_id, user_id = world["org_id"], world["user_id"]
    sessions = _sessions(org_id, user_id)
    product_service = ProductService(SqlProductRepository(), sessions, _NoopAuditLog(),
                                     SqlWarehouseRepository(), SqlUnitRepository())

    data = ProductCreate(sku="CHAIN-EXCISE", name="Excise Widget", unit_id=world["unit_id"],
                         product_type=ProductType.GOODS, purchase_price=Decimal("10"),
                         selling_price=Decimal("100"), tax_percent=Decimal("10"),
                         excise_percent=Decimal("5"))
    product, _opening = product_service.create_product(
        data, warehouse_id=world["warehouse_id"], opening_quantity=Decimal("50"))

    sales_repo = SqlSalesOrderRepository()
    so = sales_repo.create(org_id, SalesOrderCreate(
        customer_id=world["customer_id"], warehouse_id=world["warehouse_id"],
        items=[SalesOrderItemInput(product_id=product.id, quantity_ordered=Decimal("10"),
                                  unit_price=Decimal("100"), tax_percent=Decimal("10"),
                                  excise_percent=Decimal("5"))]),
        user_id)

    from app.schemas.sales import FinalizeSaleRequest
    result = sales_repo.finalize_new_bill(org_id, so.id, user_id, FinalizeSaleRequest())

    # 10 x 100 = 1000 subtotal, no discount. 10% tax -> 100. 5% excise -> 50.
    # total = 1000 + 100 + 50 = 1150.
    assert result.invoice.tax_amount == Decimal("100.00")
    assert result.invoice.excise_amount == Decimal("50.00")
    assert result.invoice.total_amount == Decimal("1150.00")


def test_delivery_date_reference_number_and_custom_fields_round_trip(world):
    """A sales order's new fields must survive a real create -> get_by_id
    round trip through the JSONB/scalar columns, untouched — not just the
    ORM in-memory object create() itself returns.
    """
    import datetime as dt

    org_id, user_id = world["org_id"], world["user_id"]
    product_service = ProductService(SqlProductRepository(), _sessions(org_id, user_id),
                                     _NoopAuditLog(), SqlWarehouseRepository(),
                                     SqlUnitRepository())
    data = ProductCreate(sku="CHAIN-REF", name="Ref Widget", unit_id=world["unit_id"],
                         product_type=ProductType.GOODS, purchase_price=Decimal("10"),
                         selling_price=Decimal("20"), tax_percent=Decimal("0"))
    product, _opening = product_service.create_product(data)

    sales_repo = SqlSalesOrderRepository()
    delivery = dt.date(2026, 11, 20)
    so = sales_repo.create(org_id, SalesOrderCreate(
        customer_id=world["customer_id"], warehouse_id=world["warehouse_id"],
        delivery_date=delivery, reference_number="CHAIN-PO-1",
        custom_fields={"delivery_window": "morning", "gate": "3"},
        items=[SalesOrderItemInput(product_id=product.id, quantity_ordered=Decimal("1"),
                                  unit_price=Decimal("20"), tax_percent=Decimal("0"))]),
        user_id)

    fetched = sales_repo.get_by_id(org_id, so.id)
    assert fetched.delivery_date == delivery
    assert fetched.reference_number == "CHAIN-PO-1"
    assert fetched.custom_fields == {"delivery_window": "morning", "gate": "3"}


class _NoopAuditLog:
    def record(self, **kwargs):
        pass
