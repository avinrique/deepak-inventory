"""Regression tests for the Stock In / Adjust Stock workflow on
InventoryPage.

Root cause of the reported bug: AsyncContentArea only invokes its
``render`` callback when ``is_empty(data)`` is False (see
app.ui.widgets.async_content). InventoryPage's ``is_empty`` is based on
the *inventory levels* list, which is empty for any organization/warehouse
that has never recorded a Stock In — the single most common real-world
state, since a level row only starts existing after a first Stock In.
Before this fix, self._products/self._warehouses were populated only as a
side effect of the levels table's render callback (_render_table), so on
that "zero levels" state the side effect never ran and both lists stayed
permanently empty. Opening the Stock In dialog then showed "No
products/warehouses available" with a permanently disabled Add Stock
button, and there was no way to ever perform the first Stock In through
the UI — a real deadlock, not a cosmetic issue.

The fix gives InventoryPage its own independent reference-data loader
(_fetch_reference_data/_load_reference_data/_on_reference_data_loaded),
the same convention already used by PurchasesPage/SalesOrdersPage, so
product/warehouse loading no longer depends on the levels table ever
having a non-empty result.

Uses MagicMock services (no database) + a real SessionManager (full
permissions) — same combination as tests/ui/test_build_page_smoke.py.
_fetch_reference_data/_on_reference_data_loaded are called directly rather
than through QThreadPool, matching the documented reasoning in
tests/ui/test_inactive_party_filtering.py and
tests/workers/test_async_content_area.py for why a threading-dependent
version of a test like this would be flaky here.
"""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

try:
    from PySide6.QtWidgets import QApplication
except ImportError:
    pytest.skip("PySide6 not available", allow_module_level=True)

from app.schemas.inventory import InventoryTransactionOut, InventoryTransactionType, WarehouseOut
from app.schemas.product import ProductOut, ProductPage
from app.security.session import SessionManager


@pytest.fixture(scope="module")
def qapp():
    try:
        return QApplication.instance() or QApplication([])
    except Exception as exc:  # noqa: BLE001 - e.g. no display available
        pytest.skip(f"cannot create QApplication: {exc}")


def _now():
    return datetime.now(timezone.utc)


def _product(name="Widget", sku="SKU-1") -> ProductOut:
    from app.domain.product import ProductStatus
    from app.schemas.product import UnitOut
    unit = UnitOut(id=uuid.uuid4(), name="Piece", abbreviation="pc")
    return ProductOut(id=uuid.uuid4(), sku=sku, barcode=None, name=name, description=None,
                      category=None, brand=None, unit=unit,
                      purchase_price=Decimal("10"), selling_price=Decimal("15"),
                      tax_percent=Decimal("13"), minimum_stock_level=Decimal("0"),
                      status=ProductStatus.ACTIVE, created_at=_now(), updated_at=_now())


def _warehouse(code="MAIN") -> WarehouseOut:
    return WarehouseOut(id=uuid.uuid4(), code=code, name=f"Warehouse {code}", address=None,
                        is_active=True, created_at=_now(), updated_at=_now())


def _fully_permissioned_sessions() -> SessionManager:
    sessions = SessionManager(idle_timeout=timedelta(minutes=30))
    sessions.start(user_id=uuid.uuid4(), organization_id=uuid.uuid4(), role_id=uuid.uuid4(),
                   permissions=frozenset(), is_superuser=True, must_change_password=False,
                   now=_now())
    return sessions


def _page(qapp, products, warehouses, levels=None):
    from app.ui.pages.inventory_page import InventoryPage

    product_service = MagicMock()
    product_service.search_products.return_value = ProductPage(
        items=products, total=len(products), page=1, page_size=500)
    inventory_service = MagicMock()
    inventory_service.list_warehouses.return_value = warehouses
    inventory_service.list_all_levels.return_value = levels or []
    stock_service = MagicMock()

    page = InventoryPage(stock_service, inventory_service, product_service,
                         _fully_permissioned_sessions())
    return page, product_service, inventory_service


def test_adjust_and_stock_in_buttons_start_disabled_until_reference_data_loads(qapp):
    page, _products_svc, _inv_svc = _page(qapp, [_product()], [_warehouse()])

    assert page._stock_in_button.isEnabled() is False
    assert page._adjust_button.isEnabled() is False

    page._on_reference_data_loaded(page._fetch_reference_data())

    assert page._stock_in_button.isEnabled() is True
    assert page._adjust_button.isEnabled() is True


def test_reference_data_error_re_enables_buttons_with_a_tooltip(qapp):
    page, _products_svc, _inv_svc = _page(qapp, [_product()], [_warehouse()])

    page._on_reference_data_error(RuntimeError("boom"))

    assert page._stock_in_button.isEnabled() is True
    assert page._stock_in_button.toolTip() != ""


def test_products_and_warehouses_populate_even_when_inventory_levels_list_is_empty(qapp):
    """The core regression test. list_all_levels() returns [] — the exact
    "never had a Stock In yet" state that used to leave self._products/
    self._warehouses permanently empty because _render_table (their only
    prior source) is skipped by AsyncContentArea whenever the levels list
    is empty.
    """
    product = _product()
    warehouse = _warehouse()
    page, _products_svc, _inv_svc = _page(qapp, [product], [warehouse], levels=[])

    assert page._products == []
    assert page._warehouses == []

    # Simulate the levels table's own load completing with an empty
    # result — exactly what AsyncContentArea does internally, and exactly
    # the path that used to be the only place self._products/
    # self._warehouses got set.
    page._async_area._on_loaded(([], [product], [warehouse]), page._async_area._generation)

    # Before the fix: still [] here, because _render_table (and its
    # self._products = products side effect) is never called for an empty
    # result. After the fix: unaffected by this call at all — populated
    # independently below.
    page._on_reference_data_loaded(page._fetch_reference_data())

    assert [p.id for p in page._products] == [product.id]
    assert [w.id for w in page._warehouses] == [warehouse.id]


def test_stock_in_dialog_has_usable_pickers_when_no_inventory_levels_exist_yet(qapp):
    """End-to-end through the real dialog widget: reproduces exactly what
    a first-time user sees when clicking "+ Stock In" before ever
    recording stock. Before the fix, both combo boxes were empty and the
    Add Stock button was permanently disabled.
    """
    from app.ui.widgets.stock_in_dialog import StockInDialog

    product = _product(name="First Ever Product")
    warehouse = _warehouse(code="MAIN")
    page, _products_svc, inventory_service = _page(qapp, [product], [warehouse], levels=[])
    page._on_reference_data_loaded(page._fetch_reference_data())

    dialog = StockInDialog(inventory_service, page._products, page._warehouses)

    assert dialog._product.count() == 1
    assert dialog._warehouse.count() == 1
    assert dialog._submit_button.isEnabled() is True
    assert dialog._product.currentData() == product.id
    assert dialog._warehouse.currentData() == warehouse.id


def test_stock_in_confirmation_and_refresh_use_the_independently_loaded_lists(qapp, monkeypatch):
    from app.ui.pages import inventory_page as inventory_page_module

    product = _product(name="Widget")
    warehouse = _warehouse(code="MAIN")
    page, _products_svc, inventory_service = _page(qapp, [product], [warehouse], levels=[])
    page._on_reference_data_loaded(page._fetch_reference_data())

    transaction = InventoryTransactionOut(
        id=uuid.uuid4(), product_id=product.id, warehouse_id=warehouse.id,
        transaction_type=InventoryTransactionType.STOCK_IN, quantity_change=Decimal("10"),
        quantity_on_hand_after=Decimal("10"), quantity_reserved_after=Decimal("0"),
        reference_type=None, reference_id=None, performed_by=uuid.uuid4(), notes=None,
        created_at=_now())

    # QMessageBox.information() is a real modal dialog — blocks forever
    # under a test with no running event loop to dismiss it. Mocked so this
    # test can assert on the message content instead of hanging.
    shown = {}
    monkeypatch.setattr(
        inventory_page_module.QMessageBox, "information",
        lambda parent, title, text: shown.update(title=title, text=text))

    page._show_stock_in_confirmation(transaction)

    # Relies on page._products/page._warehouses for the human-readable
    # names — only populated by the fix under test. Before the fix, these
    # were empty and the message would have fallen back to raw UUIDs.
    assert "Widget" in shown["text"]
    assert "Warehouse MAIN" in shown["text"]
