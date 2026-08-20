"""Regression tests: inactive customers/suppliers/warehouses must not
appear in the pickers used to create a brand new transaction (New Bill,
Sales Orders, Purchases). Before this fix, list_customers()/
list_suppliers()/list_warehouses() were used unfiltered — a deactivated
customer was still selectable for a new bill despite
CustomersPage._deactivate's UI copy explicitly promising otherwise.

Exercises the extracted _fetch_reference_data/_fetch_filter_options
methods directly (no QThreadPool involved) — the same reasoning
tests/workers/test_async_content_area.py documents for why a
threading-dependent version of a test like this would be flaky here.
"""
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

try:
    from PySide6.QtWidgets import QApplication
except ImportError:
    pytest.skip("PySide6 not available", allow_module_level=True)

from app.schemas.inventory import WarehouseOut
from app.schemas.purchasing import SupplierOut
from app.schemas.sales import CustomerOut


@pytest.fixture(scope="module")
def qapp():
    try:
        return QApplication.instance() or QApplication([])
    except Exception as exc:  # noqa: BLE001 - e.g. no display available
        pytest.skip(f"cannot create QApplication: {exc}")


def _now():
    return datetime.now(timezone.utc)


def _warehouse(is_active: bool, code: str = "WH1") -> WarehouseOut:
    return WarehouseOut(id=uuid.uuid4(), code=code, name=f"Warehouse {code}", address=None,
                        is_active=is_active, created_at=_now(), updated_at=_now())


def _supplier(is_active: bool, name: str = "Acme") -> SupplierOut:
    return SupplierOut(id=uuid.uuid4(), name=name, contact_person=None, phone=None, email=None,
                       address=None, tax_id=None, notes=None, is_active=is_active,
                       created_at=_now(), updated_at=_now())


def _customer(is_active: bool, name: str = "Jane") -> CustomerOut:
    from app.domain.sales import CustomerType
    return CustomerOut(id=uuid.uuid4(), customer_code=None, name=name,
                       customer_type=CustomerType.INDIVIDUAL, contact_person=None, phone=None,
                       email=None, address=None, city=None, state=None, country=None,
                       tax_id=None, credit_limit=None, opening_balance=0, notes=None,
                       is_active=is_active, created_by=uuid.uuid4(), created_at=_now(),
                       updated_at=_now())


def test_new_bill_page_excludes_inactive_customers_and_warehouses(qapp):
    from app.ui.pages.new_bill_page import NewBillPage

    sales_service = MagicMock()
    sales_service.list_customers.return_value = [_customer(True, "Active Co"),
                                                  _customer(False, "Retired Co")]
    inventory_service = MagicMock()
    inventory_service.list_warehouses.return_value = [_warehouse(True, "MAIN"),
                                                       _warehouse(False, "OLD")]
    product_service = MagicMock()
    sessions = MagicMock()

    page = NewBillPage(sales_service, inventory_service, product_service, sessions)
    customers, warehouses = page._fetch_reference_data()

    assert [c.name for c in customers] == ["Active Co"]
    assert [w.code for w in warehouses] == ["MAIN"]


def test_sales_orders_page_excludes_inactive_customers_and_warehouses(qapp):
    from app.schemas.product import ProductPage
    from app.ui.pages.sales_orders_page import SalesOrdersPage

    sales_service = MagicMock()
    sales_service.list_customers.return_value = [_customer(True, "Active Co"),
                                                  _customer(False, "Retired Co")]
    inventory_service = MagicMock()
    inventory_service.list_warehouses.return_value = [_warehouse(True, "MAIN"),
                                                       _warehouse(False, "OLD")]
    product_service = MagicMock()
    product_service.search_products.return_value = ProductPage(items=[], total=0, page=1,
                                                                page_size=500)
    sessions = MagicMock()

    page = SalesOrdersPage(sales_service, inventory_service, product_service, sessions)
    customers, warehouses, _products = page._fetch_filter_options()

    assert [c.name for c in customers] == ["Active Co"]
    assert [w.code for w in warehouses] == ["MAIN"]


def test_purchases_page_excludes_inactive_suppliers_and_warehouses(qapp):
    from app.schemas.product import ProductPage
    from app.ui.pages.purchases_page import PurchasesPage

    purchase_service = MagicMock()
    purchase_service.list_suppliers.return_value = [_supplier(True, "Active Supplier"),
                                                     _supplier(False, "Retired Supplier")]
    inventory_service = MagicMock()
    inventory_service.list_warehouses.return_value = [_warehouse(True, "MAIN"),
                                                       _warehouse(False, "OLD")]
    product_service = MagicMock()
    product_service.search_products.return_value = ProductPage(items=[], total=0, page=1,
                                                                page_size=500)
    sessions = MagicMock()

    page = PurchasesPage(purchase_service, inventory_service, product_service, sessions)
    suppliers, warehouses, _products = page._fetch_reference_data()

    assert [s.name for s in suppliers] == ["Active Supplier"]
    assert [w.code for w in warehouses] == ["MAIN"]


# -- new Warehouses/Suppliers pages (previously placeholders) -------------#

def test_warehouses_page_constructs_and_renders_rows(qapp):
    from app.ui.pages.warehouses_page import WarehousesPage

    inventory_service = MagicMock()
    inventory_service.list_warehouses.return_value = [_warehouse(True, "MAIN")]
    sessions = MagicMock()
    sessions.current.return_value = MagicMock(
        permissions={"inventory.view", "warehouse.manage"}, is_superuser=False)

    page = WarehousesPage(inventory_service, sessions)
    table = page._render_table(inventory_service.list_warehouses.return_value)
    assert table.rowCount() == 1
    assert table.item(0, 0).text() == "MAIN"


def test_suppliers_page_constructs_and_renders_rows(qapp):
    from app.ui.pages.suppliers_page import SuppliersPage

    purchase_service = MagicMock()
    purchase_service.list_suppliers.return_value = [_supplier(True, "Acme")]
    sessions = MagicMock()
    sessions.current.return_value = MagicMock(
        permissions={"purchases.view", "purchases.update"}, is_superuser=False)

    page = SuppliersPage(purchase_service, sessions)
    table = page._render_table(purchase_service.list_suppliers.return_value)
    assert table.rowCount() == 1
    assert table.item(0, 0).text() == "Acme"
