"""TransactionItemsTable — column layout, row math, and collected-item
shape for the shared widget New Bill/Sales Order/Purchase Order all embed.
add_row()/collect_items()/compute_totals() are called directly, no event
loop needed — the async stock refresh short-circuits synchronously
(returns "—") whenever no warehouse has been set (see
TransactionItemsTable._refresh_stock), so no QThreadPool worker actually
runs, and a bare object() stand-in is enough for product_service/
inventory_service. Row removal is deliberately not exercised here — see
the note above where that test would go.
"""
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from PySide6.QtWidgets import QApplication

from app.domain.product import ProductStatus, ProductType
from app.schemas.product import ProductOut, UnitOut
from app.ui.widgets.transaction_items_table import (
    _COL_HSN,
    _COL_QTY,
    TransactionItemsTable,
)


@pytest.fixture(scope="module")
def qapp():
    try:
        return QApplication.instance() or QApplication([])
    except Exception as exc:  # noqa: BLE001 - e.g. no display available
        pytest.skip(f"cannot create QApplication: {exc}")


def _product(sku="SKU-1", name="Widget", selling_price=Decimal("50"),
            purchase_price=Decimal("30"), tax_percent=Decimal("13"),
            excise_percent=Decimal("5"), hsn_code="1234") -> ProductOut:
    now = datetime.now(timezone.utc)
    unit = UnitOut(id=uuid.uuid4(), name="Piece", abbreviation="pc")
    return ProductOut(id=uuid.uuid4(), sku=sku, barcode=None, name=name,
                      description=None, product_type=ProductType.GOODS, category=None,
                      brand=None, unit=unit, sub_unit=None, sub_unit_conversion_factor=None,
                      tertiary_unit=None, tertiary_unit_conversion_factor=None,
                      purchase_price=purchase_price, selling_price=selling_price,
                      tax_percent=tax_percent, is_taxable=True, excise_percent=excise_percent,
                      minimum_stock_level=Decimal("0"), hsn_code=hsn_code, size=None,
                      color=None, flavour=None, dftqc_no=None, country_of_origin=None,
                      expiry_date=None, status=ProductStatus.ACTIVE, created_at=now,
                      updated_at=now)


def _table(qapp, *, include_discount=True, price_field="selling_price"):
    return TransactionItemsTable(object(), object(), include_discount=include_discount,
                                 price_label="Rate", price_field=price_field)


# -- column layout --------------------------------------------------------- #

def test_columns_with_discount_include_hsn_discount_and_excise(qapp):
    table = _table(qapp, include_discount=True)
    headers = [table._table.horizontalHeaderItem(i).text()
              for i in range(table._table.columnCount())]
    assert headers == ["#", "Item/Product", "SKU", "HSN Code", "Qty", "Rate",
                       "Discount %", "Excise %", "Tax %", "Amount", "Stock", "Action"]


def test_columns_without_discount_omit_discount_and_excise(qapp):
    table = _table(qapp, include_discount=False, price_field="purchase_price")
    headers = [table._table.horizontalHeaderItem(i).text()
              for i in range(table._table.columnCount())]
    assert headers == ["#", "Item/Product", "SKU", "HSN Code", "Qty", "Rate",
                       "Tax %", "Amount", "Stock", "Action"]
    assert table._col_discount is None
    assert table._col_excise is None


# -- add_row / HSN column ---------------------------------------------------#

def test_add_row_populates_hsn_code_column(qapp):
    table = _table(qapp)
    table.add_row(_product(hsn_code="8471"))
    assert table._table.item(0, _COL_HSN).text() == "8471"


def test_add_row_shows_em_dash_when_hsn_code_missing(qapp):
    table = _table(qapp)
    table.add_row(_product(hsn_code=None))
    assert table._table.item(0, _COL_HSN).text() == "—"


def test_add_row_seeds_excise_field_from_product(qapp):
    table = _table(qapp)
    table.add_row(_product(excise_percent=Decimal("7.5")))
    excise_widget = table._table.cellWidget(0, table._col_excise)
    assert excise_widget.text() == "7.5"


# -- collect_items ------------------------------------------------------------#

def test_collect_items_includes_excise_percent_when_discount_enabled(qapp):
    table = _table(qapp)
    table.add_row(_product(excise_percent=Decimal("5")))
    items, errors = table.collect_items()
    assert errors == []
    assert items[0]["excise_percent"] == Decimal("5")
    assert items[0]["discount_percent"] == Decimal("0")


def test_collect_items_omits_excise_percent_when_discount_disabled(qapp):
    table = _table(qapp, include_discount=False, price_field="purchase_price")
    table.add_row(_product())
    items, errors = table.collect_items()
    assert errors == []
    assert "excise_percent" not in items[0]
    assert "discount_percent" not in items[0]


def test_collect_items_rejects_non_numeric_excise(qapp):
    table = _table(qapp)
    table.add_row(_product())
    excise_widget = table._table.cellWidget(0, table._col_excise)
    excise_widget.setText("not-a-number")
    items, errors = table.collect_items()
    assert items == []
    assert any("excise" in e.lower() for e in errors)


# -- compute_totals -----------------------------------------------------------#

def test_compute_totals_returns_five_tuple_with_correct_excise_math(qapp):
    table = _table(qapp)
    # 10 units x $100 = $1000 subtotal, no discount, 10% tax -> $100,
    # 5% excise -> $50. Grand total = 1000 + 100 + 50 = 1150.
    table.add_row(_product(selling_price=Decimal("100"), tax_percent=Decimal("10"),
                           excise_percent=Decimal("5")))
    qty_widget = table._table.cellWidget(0, _COL_QTY)
    qty_widget.setText("10")

    subtotal, discount_total, tax_total, excise_total, grand_total = table.compute_totals()
    assert subtotal == Decimal("1000")
    assert discount_total == Decimal("0")
    assert tax_total == Decimal("100")
    assert excise_total == Decimal("50")
    assert grand_total == Decimal("1150")


def test_compute_totals_excise_always_zero_when_discount_disabled(qapp):
    table = _table(qapp, include_discount=False, price_field="purchase_price")
    table.add_row(_product(tax_percent=Decimal("10")))
    _subtotal, _discount, _tax, excise_total, _grand = table.compute_totals()
    assert excise_total == Decimal("0")


# NOTE: row removal (_remove_row_widget / the "Remove" button) is
# deliberately NOT exercised here. Both `remove_button.click()` and a
# direct `table._remove_row_widget(remove_button)` call reproducibly
# segfault this environment's PySide6/shiboken binding when combined with
# tests/workers/test_worker_multi_slot_delivery.py later in the same test
# process (confirmed via bisection — the crash relocates to that unrelated
# test's own event-loop pump, well after this test's objects would have
# been garbage-collected, so it is not simply "call processEvents() after
# the click"). Removal logic itself is unchanged by this feature (no new
# code path — HSN/excise columns are handled the same as every other
# column in _remove_row_widget/_renumber_rows), so the missing automated
# coverage here is a pre-existing gap in the test harness's Qt-lifetime
# handling, not a regression risk from this change.
