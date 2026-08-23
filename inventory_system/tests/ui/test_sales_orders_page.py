"""SalesOrdersPage — the pure/directly-testable pieces of the sales
register page. Mirrors tests/ui/test_purchases_page.py's structure and
rationale; see that module's docstring for what's deliberately NOT covered
here (full async-load rendering, exercised manually against a live
database instead).
"""
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

try:
    from PySide6.QtWidgets import QApplication
except ImportError:
    pytest.skip("PySide6 not available", allow_module_level=True)

from app.domain.product import ProductStatus
from app.domain.sales import SalesOrderStatus
from app.schemas.product import ProductPage
from app.schemas.transactions import TransactionListPage, TransactionListRow, TransactionTotals
from app.security.session import SessionManager
from app.ui.pages.sales_orders_page import (
    SalesOrdersPage,
    _export_columns,
    _row_to_export_dict,
    hs_code_summary,
)


@pytest.fixture(scope="module")
def qapp():
    try:
        return QApplication.instance() or QApplication([])
    except Exception as exc:  # noqa: BLE001 - e.g. no display available
        pytest.skip(f"cannot create QApplication: {exc}")


def _sessions(permissions=frozenset()):
    sessions = SessionManager(idle_timeout=timedelta(minutes=30))
    sessions.start(user_id=uuid.uuid4(), organization_id=uuid.uuid4(), role_id=uuid.uuid4(),
                   permissions=frozenset(permissions), is_superuser=False,
                   must_change_password=False, now=datetime.now(timezone.utc))
    return sessions


def _empty_page() -> TransactionListPage:
    return TransactionListPage(items=[], totals=TransactionTotals(), total=0, page=1,
                               page_size=25)


def _row(**overrides) -> TransactionListRow:
    defaults = dict(id=uuid.uuid4(), status=SalesOrderStatus.DRAFT.value,
                    created_at=datetime(2026, 3, 4, tzinfo=timezone.utc),
                    order_number=None, invoice_number="INV-000001", reference_number="PO-77",
                    party_name="Jane Buyer", hs_codes=["1001"],
                    taxable_amount=Decimal("100"), non_taxable_amount=Decimal("0"),
                    vat_amount=Decimal("13"), excise_amount=Decimal("5"),
                    total_amount=Decimal("118"))
    defaults.update(overrides)
    return TransactionListRow(**defaults)


def _page(qapp, *, permissions=frozenset({"sales.view"}), customers=None, warehouses=None):
    sales_service = MagicMock()
    sales_service.list_customers.return_value = customers or []
    sales_service.list_sales_transactions.return_value = _empty_page()
    inventory_service = MagicMock()
    inventory_service.list_warehouses.return_value = warehouses or []
    product_service = MagicMock()
    product_service.search_products.return_value = ProductPage(items=[], total=0, page=1,
                                                                page_size=500)
    page = SalesOrdersPage(sales_service, inventory_service, product_service,
                           _sessions(permissions))
    return page, sales_service, inventory_service, product_service


# -- hs_code_summary (identical rule to the purchases page) ------------------#

def test_hs_code_summary_empty():
    assert hs_code_summary([]) == ("—", "")


def test_hs_code_summary_multiple_codes():
    text, tooltip = hs_code_summary(["1001", "2002"])
    assert text == "1001 +1 more"
    assert tooltip == "1001, 2002"


# -- export row shaping, including the sales-only excise column --------------#

def test_export_columns_include_excise_and_match_row_dict_keys():
    row = _row()
    row_dict = _row_to_export_dict(row)
    assert "Excise Amount" in _export_columns()
    assert list(row_dict.keys()) == _export_columns()


def test_row_to_export_dict_carries_excise_amount(qapp):
    row = _row(excise_amount=Decimal("42.50"))
    assert _row_to_export_dict(row)["Excise Amount"] == Decimal("42.50")


# -- _build_filter ------------------------------------------------------------#

def test_build_filter_defaults_to_no_bounds(qapp):
    page, *_ = _page(qapp)
    filt = page._build_filter()
    assert filt.search is None
    assert filt.date_from is None
    assert filt.date_to is None
    assert filt.customer_id is None
    assert filt.status is None
    assert filt.sort_by == "created_at"
    assert filt.sort_desc is True


def test_build_filter_reflects_search_text(qapp):
    page, *_ = _page(qapp)
    page._search.setText("  INV-9911  ")
    assert page._build_filter().search == "INV-9911"


def test_build_filter_reflects_status_selection(qapp):
    page, *_ = _page(qapp)
    index = page._status_filter.findData(SalesOrderStatus.CONFIRMED)
    page._status_filter.setCurrentIndex(index)
    assert page._build_filter().status == SalesOrderStatus.CONFIRMED


def test_build_filter_reflects_date_range(qapp):
    page, *_ = _page(qapp)
    page._date_range._from_check.setChecked(True)
    page._date_range._from_edit.setDate(page._date_range._from_edit.date().__class__(
        2026, 2, 1))
    page._date_range._to_check.setChecked(True)
    page._date_range._to_edit.setDate(page._date_range._to_edit.date().__class__(2026, 2, 28))
    filt = page._build_filter()
    assert filt.date_from == date(2026, 2, 1)
    assert filt.date_to == date(2026, 2, 28)


# -- sort click behavior -------------------------------------------------------#

def test_sort_click_on_unsortable_column_is_ignored(qapp):
    page, *_ = _page(qapp)
    page._page = 2
    page._on_sort_clicked(1)  # H.S Code column
    assert page._sort_by == "created_at"
    assert page._page == 2


def test_sort_click_on_money_column_defaults_descending_and_resets_page(qapp):
    page, *_ = _page(qapp)
    page._page = 3
    page._on_sort_clicked(8)  # Amount column
    assert page._sort_by == "total_amount"
    assert page._sort_desc is True
    assert page._page == 1


def test_sort_click_on_text_column_defaults_ascending(qapp):
    page, *_ = _page(qapp)
    page._on_sort_clicked(4)  # Customer column
    assert page._sort_by == "party_name"
    assert page._sort_desc is False


def test_sort_click_twice_on_same_column_toggles_direction(qapp):
    page, *_ = _page(qapp)
    page._on_sort_clicked(8)
    assert page._sort_desc is True
    page._on_sort_clicked(8)
    assert page._sort_desc is False


# -- filter options fetch (mirrors the existing customers/warehouses pattern) #

def test_fetch_filter_options_excludes_inactive_customers_and_warehouses(qapp):
    active_customer = MagicMock(is_active=True)
    inactive_customer = MagicMock(is_active=False)
    active_warehouse = MagicMock(is_active=True)
    inactive_warehouse = MagicMock(is_active=False)
    page, sales_service, inventory_service, product_service = _page(
        qapp, customers=[active_customer, inactive_customer],
        warehouses=[active_warehouse, inactive_warehouse])

    customers, warehouses, products = page._fetch_filter_options()
    assert customers == [active_customer]
    assert warehouses == [active_warehouse]
    product_service.search_products.assert_called_once()
    filt = product_service.search_products.call_args[0][0]
    assert filt.status == ProductStatus.ACTIVE
