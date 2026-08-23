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
    _COL_PRICE,
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


# -- add_row overrides (seeding an existing order's saved values) --------- #

def test_add_row_override_wins_over_product_defaults(qapp):
    """A saved order line's price/tax reflect what was actually agreed —
    they must not be silently replaced by the product's current catalog
    values (which may have changed since).
    """
    table = _table(qapp)
    product = _product(selling_price=Decimal("50"), tax_percent=Decimal("13"))
    table.add_row(product, quantity=Decimal("3"), unit_price=Decimal("42.50"),
                 tax_percent=Decimal("0"), discount_percent=Decimal("10"),
                 excise_percent=Decimal("2"))

    assert table._table.cellWidget(0, _COL_QTY).text() == "3"
    assert table._table.cellWidget(0, _COL_PRICE).text() == "42.50"
    assert table._table.cellWidget(0, table._col_tax).text() == "0"
    assert table._table.cellWidget(0, table._col_discount).text() == "10"
    assert table._table.cellWidget(0, table._col_excise).text() == "2"


def test_add_row_with_no_overrides_still_uses_product_defaults(qapp):
    table = _table(qapp)
    table.add_row(_product(tax_percent=Decimal("13")))
    assert table._table.cellWidget(0, table._col_tax).text() == "13"


# -- set_items -------------------------------------------------------------- #

def test_set_items_replaces_existing_rows(qapp):
    table = _table(qapp)
    table.add_row(_product(name="Old Product"))
    assert table._table.rowCount() == 1

    new_product = _product(name="New Product", sku="SKU-2")
    table.set_items([(new_product, {"quantity": Decimal("5"),
                                    "unit_price": Decimal("20"),
                                    "tax_percent": Decimal("13"),
                                    "discount_percent": Decimal("0"),
                                    "excise_percent": Decimal("0")})])

    assert table._table.rowCount() == 1
    assert table._table.item(0, 1).text() == "New Product"
    assert table._table.cellWidget(0, _COL_QTY).text() == "5"


def test_set_items_with_empty_list_clears_the_table(qapp):
    table = _table(qapp)
    table.add_row(_product())
    table.set_items([])
    assert table._table.rowCount() == 0
    assert table.is_empty()


def test_set_items_round_trips_through_collect_items(qapp):
    table = _table(qapp)
    product = _product()
    table.set_items([(product, {"quantity": Decimal("7"), "unit_price": Decimal("99.99"),
                                "tax_percent": Decimal("13"), "discount_percent": Decimal("5"),
                                "excise_percent": Decimal("1")})])

    items, errors = table.collect_items()
    assert errors == []
    assert items[0]["quantity"] == Decimal("7")
    assert items[0]["unit_price"] == Decimal("99.99")
    assert items[0]["discount_percent"] == Decimal("5")
    assert items[0]["excise_percent"] == Decimal("1")


# -- set_read_only ------------------------------------------------------------#

def test_read_only_disables_search_and_closes_the_popup(qapp):
    table = _table(qapp)
    table._popup.show_popup()
    table.set_read_only(True)
    assert table._search.isEnabled() is False
    assert table._popup.is_open() is False


def test_read_only_makes_existing_row_fields_read_only(qapp):
    table = _table(qapp)
    table.add_row(_product())
    table.set_read_only(True)

    assert table._table.cellWidget(0, _COL_QTY).isReadOnly() is True
    assert table._table.cellWidget(0, table._col_tax).isReadOnly() is True
    assert table._table.cellWidget(0, table._col_action).isVisible() is False


def test_read_only_applied_to_rows_added_after_the_call(qapp):
    """set_items() (used to seed View/Edit) calls add_row() after
    set_read_only() may already be in effect on a reused widget instance
    — new rows must come in already locked, not just the ones present at
    the time set_read_only() was called.
    """
    table = _table(qapp)
    table.set_read_only(True)
    table.add_row(_product())
    assert table._table.cellWidget(0, _COL_QTY).isReadOnly() is True


# -- product-creation opt-in ------------------------------------------------#

def test_add_product_creation_disabled_by_default(qapp):
    """allow_product_creation defaults to False — the Sales/Purchase Order
    forms must keep behaving exactly as before this feature until their
    pages explicitly opt in (see TransactionItemsTable's docstring)."""
    table = _table(qapp)
    assert table._allow_product_creation is False


def test_popup_add_product_row_hidden_when_creation_disabled(qapp):
    table = _table(qapp)
    table._popup.show_products([], "selling_price", None,
                               show_add_product=table._allow_product_creation)
    assert table._popup._list.count() == 1  # only the "No product found" message


def test_popup_add_product_row_shown_when_creation_enabled(qapp):
    table = TransactionItemsTable(object(), object(), allow_product_creation=True)
    table._popup.show_products([], "selling_price", None,
                               show_add_product=table._allow_product_creation)
    assert table._popup._list.count() == 2  # message + "+ Add Product"


def test_activating_add_product_row_emits_add_product_requested_with_search_text(qapp):
    table = TransactionItemsTable(object(), object(), allow_product_creation=True)
    table._search.setText("nonexistent widget")
    received = []
    table.add_product_requested.connect(received.append)

    table._on_popup_add_product_requested()

    assert received == ["nonexistent widget"]


def test_add_product_requested_never_fires_when_creation_disabled(qapp):
    """The popup itself never renders the row when show_add_product=False
    (see the tests above); this pins the signal side too — even if
    something did activate it, nothing upstream should be listening for a
    disabled feature to matter, but the emit still shouldn't happen."""
    table = _table(qapp)  # allow_product_creation=False
    received = []
    table.add_product_requested.connect(received.append)
    table._popup.show_products([], "selling_price", None, show_add_product=False)
    table._popup.activate_selection()
    assert received == []


def test_selecting_a_suggestion_adds_a_row_and_resets_the_search_box(qapp):
    table = _table(qapp)
    product = _product(name="Selected Widget")
    table._search.setText("widget")

    table._on_suggestion_selected(product)

    assert table._table.rowCount() == 1
    assert table._table.item(0, 1).text() == "Selected Widget"
    assert table._search.text() == ""
    assert table._popup.is_open() is False


# -- soft over-stock warning (never blocks) ---------------------------------#

def test_stock_warning_is_absent_before_stock_has_loaded(qapp):
    table = _table(qapp)
    table.add_row(_product(), quantity=Decimal("999"))
    # No warehouse was set, so "available" stays None — no I/O happened,
    # and there's nothing yet to compare the quantity against.
    assert table._rows[0]["available"] is None
    assert table._table.item(0, table._col_stock).toolTip() == ""


def test_stock_warning_appears_when_quantity_exceeds_available(qapp):
    table = _table(qapp)
    table.add_row(_product(), quantity=Decimal("10"))
    table._rows[0]["available"] = Decimal("4")

    table._apply_stock_warning(0)

    stock_item = table._table.item(0, table._col_stock)
    assert stock_item.toolTip() != ""
    assert "4" in stock_item.toolTip()


def test_stock_warning_clears_once_quantity_no_longer_exceeds_available(qapp):
    table = _table(qapp)
    table.add_row(_product(), quantity=Decimal("10"))
    table._rows[0]["available"] = Decimal("4")
    table._apply_stock_warning(0)
    assert table._table.item(0, table._col_stock).toolTip() != ""

    table._table.cellWidget(0, _COL_QTY).setText("2")  # triggers _on_row_changed

    assert table._table.item(0, table._col_stock).toolTip() == ""


def test_stock_warning_does_not_block_collect_items(qapp):
    """Purely a heads-up — an over-stock quantity must still collect
    cleanly with no error, since allow_negative_stock is an org-level
    policy enforced server-side, not a client-side gate (see the widget's
    docstring on _apply_stock_warning)."""
    table = _table(qapp)
    table.add_row(_product(), quantity=Decimal("999"))
    table._rows[0]["available"] = Decimal("1")
    table._apply_stock_warning(0)

    items, errors = table.collect_items()

    assert errors == []
    assert items[0]["quantity"] == Decimal("999")


def test_stock_warning_never_appears_for_a_quantity_at_or_below_available(qapp):
    table = _table(qapp)
    table.add_row(_product(), quantity=Decimal("4"))
    table._rows[0]["available"] = Decimal("4")

    table._apply_stock_warning(0)

    assert table._table.item(0, table._col_stock).toolTip() == ""


# -- search-result staleness guard ------------------------------------------#

def test_stale_search_result_is_discarded(qapp):
    """A slow search that returns after a newer one started must not
    clobber the newer (or empty) popup state — the same generation-guard
    shape AsyncContentArea uses for reload() races."""
    table = _table(qapp)
    table._search_generation = 5
    stale_generation = 3

    class _Page:
        items = [_product(name="Stale Result")]

    table._on_search_results((_Page(), {}), stale_generation)

    assert table._search_results == []  # never adopted


def test_current_generation_search_result_is_applied(qapp):
    table = _table(qapp)
    table._search_generation = 1

    class _Page:
        items = [_product(name="Fresh Result")]

    table._on_search_results((_Page(), {}), 1)

    assert [p.name for p in table._search_results] == ["Fresh Result"]
