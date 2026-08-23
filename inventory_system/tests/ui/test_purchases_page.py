"""PurchasesPage — the pure/directly-testable pieces of the purchase
register page: HS-code summarisation, filter construction from toolbar
state, server-side sort-click behavior, export-row shaping, and the
reference-data fetch (mirrors the "extracted so it's testable without
QThreadPool" convention already used by _fetch_reference_data in the page
itself).

Full end-to-end rendering (a real load -> AsyncContentArea -> TotalsTable
round trip) is exercised separately against a live database — see the
manual verification in the task notes and
tests/repositories/test_transaction_list_filters.py for the query
behavior this page's filter maps onto.
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
from app.domain.purchasing import PurchaseOrderStatus
from app.schemas.product import ProductPage
from app.schemas.transactions import TransactionListPage, TransactionListRow, TransactionTotals
from app.security.session import SessionManager
from app.ui.pages.purchases_page import (
    PurchasesPage,
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
    defaults = dict(id=uuid.uuid4(), status=PurchaseOrderStatus.DRAFT.value,
                    created_at=datetime(2026, 3, 4, tzinfo=timezone.utc),
                    order_number="PO-000001", invoice_number="BILL-1", reference_number="REF-1",
                    party_name="Acme Supplies", hs_codes=["1001"],
                    taxable_amount=Decimal("100"), non_taxable_amount=Decimal("0"),
                    vat_amount=Decimal("13"), total_amount=Decimal("113"))
    defaults.update(overrides)
    return TransactionListRow(**defaults)


def _page(qapp, *, permissions=frozenset({"purchases.view"}), suppliers=None, warehouses=None):
    purchase_service = MagicMock()
    purchase_service.list_suppliers.return_value = suppliers or []
    purchase_service.list_purchase_transactions.return_value = _empty_page()
    inventory_service = MagicMock()
    inventory_service.list_warehouses.return_value = warehouses or []
    product_service = MagicMock()
    product_service.search_products.return_value = ProductPage(items=[], total=0, page=1,
                                                                page_size=500)
    page = PurchasesPage(purchase_service, inventory_service, product_service,
                         _sessions(permissions))
    return page, purchase_service, inventory_service, product_service


# -- hs_code_summary --------------------------------------------------------#

def test_hs_code_summary_empty():
    assert hs_code_summary([]) == ("—", "")


def test_hs_code_summary_single_code():
    assert hs_code_summary(["1001"]) == ("1001", "1001")


def test_hs_code_summary_multiple_codes_shows_count_and_full_tooltip():
    text, tooltip = hs_code_summary(["1001", "2002", "3003"])
    assert text == "1001 +2 more"
    assert tooltip == "1001, 2002, 3003"


# -- export row shaping -------------------------------------------------------#

def test_export_columns_match_row_dict_keys():
    row = _row()
    row_dict = _row_to_export_dict(row)
    assert list(row_dict.keys()) == _export_columns()


def test_row_to_export_dict_uses_raw_decimal_not_formatted_string():
    row = _row(taxable_amount=Decimal("1234.5"))
    assert _row_to_export_dict(row)["Taxable Amount"] == Decimal("1234.5")


def test_row_to_export_dict_joins_all_hs_codes_not_just_the_summary():
    row = _row(hs_codes=["1001", "2002", "3003"])
    assert _row_to_export_dict(row)["H.S Codes"] == "1001, 2002, 3003"


# -- _build_filter ------------------------------------------------------------#

def test_build_filter_defaults_to_no_bounds(qapp):
    page, *_ = _page(qapp)
    filt = page._build_filter()
    assert filt.search is None
    assert filt.date_from is None
    assert filt.date_to is None
    assert filt.supplier_id is None
    assert filt.status is None
    assert filt.sort_by == "created_at"
    assert filt.sort_desc is True
    assert filt.page_size == 25


def test_build_filter_reflects_search_text(qapp):
    page, *_ = _page(qapp)
    page._search.setText("  BILL-9911  ")
    assert page._build_filter().search == "BILL-9911"


def test_build_filter_reflects_status_selection(qapp):
    page, *_ = _page(qapp)
    index = page._status_filter.findData(PurchaseOrderStatus.SUBMITTED)
    page._status_filter.setCurrentIndex(index)
    assert page._build_filter().status == PurchaseOrderStatus.SUBMITTED


def test_build_filter_reflects_date_range(qapp):
    page, *_ = _page(qapp)
    page._date_range._from_check.setChecked(True)
    page._date_range._from_edit.setDate(page._date_range._from_edit.date().__class__(
        2026, 1, 1))
    page._date_range._to_check.setChecked(True)
    page._date_range._to_edit.setDate(page._date_range._to_edit.date().__class__(2026, 1, 31))
    filt = page._build_filter()
    assert filt.date_from == date(2026, 1, 1)
    assert filt.date_to == date(2026, 1, 31)


def test_build_filter_unset_date_range_bounds_are_none(qapp):
    page, *_ = _page(qapp)
    assert page._build_filter().date_from is None
    assert page._build_filter().date_to is None


# -- sort click behavior -------------------------------------------------------#

def test_sort_click_on_unsortable_column_is_ignored(qapp):
    page, *_ = _page(qapp)
    page._page = 3
    page._on_sort_clicked(1)  # H.S Code column — not in _SORTABLE_COLUMNS
    assert page._sort_by == "created_at"
    assert page._page == 3  # unchanged — no refresh triggered


def test_sort_click_on_money_column_defaults_descending_and_resets_page(qapp):
    page, *_ = _page(qapp)
    page._page = 4
    page._on_sort_clicked(8)  # Amount column
    assert page._sort_by == "total_amount"
    assert page._sort_desc is True
    assert page._page == 1


def test_sort_click_on_text_column_defaults_ascending(qapp):
    page, *_ = _page(qapp)
    page._on_sort_clicked(4)  # Supplier column
    assert page._sort_by == "party_name"
    assert page._sort_desc is False


def test_sort_click_twice_on_same_column_toggles_direction(qapp):
    page, *_ = _page(qapp)
    page._on_sort_clicked(8)  # Amount — defaults desc on first click
    assert page._sort_desc is True
    page._on_sort_clicked(8)  # same column again — toggles
    assert page._sort_desc is False
    page._on_sort_clicked(8)  # toggles back
    assert page._sort_desc is True


def test_sort_click_on_already_current_column_toggles_from_initial_default(qapp):
    """The page opens already sorted by created_at/desc — clicking the Date
    header toggles that existing sort rather than reapplying the default.
    """
    page, *_ = _page(qapp)
    assert page._sort_by == "created_at" and page._sort_desc is True
    page._on_sort_clicked(0)  # Date column, same key as the initial sort
    assert page._sort_by == "created_at"
    assert page._sort_desc is False


# -- reference data fetch (mirrors the existing suppliers/warehouses pattern) #

def test_fetch_reference_data_excludes_inactive_suppliers_and_warehouses(qapp):
    active_supplier = MagicMock(is_active=True)
    inactive_supplier = MagicMock(is_active=False)
    active_warehouse = MagicMock(is_active=True)
    inactive_warehouse = MagicMock(is_active=False)
    page, purchase_service, inventory_service, product_service = _page(
        qapp, suppliers=[active_supplier, inactive_supplier],
        warehouses=[active_warehouse, inactive_warehouse])

    suppliers, warehouses, products = page._fetch_reference_data()
    assert suppliers == [active_supplier]
    assert warehouses == [active_warehouse]
    product_service.search_products.assert_called_once()
    filt = product_service.search_products.call_args[0][0]
    assert filt.status == ProductStatus.ACTIVE
