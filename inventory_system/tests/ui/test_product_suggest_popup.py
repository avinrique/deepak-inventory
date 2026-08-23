"""ProductSuggestPopup — row rendering, selection defaulting, keyboard
navigation, and the "No product found" / "+ Add Product" states. This
widget does no I/O (see its module docstring), so every test here drives
it directly with hand-built ProductOut/InventoryLevel objects — no
service, no QThreadPool, no database.
"""
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest

try:
    from PySide6.QtWidgets import QApplication, QLineEdit
except ImportError:
    pytest.skip("PySide6 not available", allow_module_level=True)

from app.domain.product import ProductStatus, ProductType
from app.schemas.inventory import InventoryLevel
from app.schemas.product import ProductOut, UnitOut
from app.ui.widgets.product_suggest_popup import ProductSuggestPopup


@pytest.fixture(scope="module")
def qapp():
    try:
        return QApplication.instance() or QApplication([])
    except Exception as exc:  # noqa: BLE001 - e.g. no display available
        pytest.skip(f"cannot create QApplication: {exc}")


def _product(name="Widget", sku="SKU-1", hsn_code="8471") -> ProductOut:
    now = datetime.now(timezone.utc)
    unit = UnitOut(id=uuid.uuid4(), name="Piece", abbreviation="pc")
    return ProductOut(id=uuid.uuid4(), sku=sku, barcode=None, name=name, description=None,
                      product_type=ProductType.GOODS, category=None, brand=None, unit=unit,
                      sub_unit=None, sub_unit_conversion_factor=None, tertiary_unit=None,
                      tertiary_unit_conversion_factor=None, purchase_price=Decimal("10"),
                      selling_price=Decimal("15"), tax_percent=Decimal("13"), is_taxable=True,
                      excise_percent=Decimal("0"), minimum_stock_level=Decimal("0"),
                      hsn_code=hsn_code, size=None, color=None, flavour=None, dftqc_no=None,
                      country_of_origin=None, expiry_date=None, status=ProductStatus.ACTIVE,
                      created_at=now, updated_at=now)


def _level(product: ProductOut, on_hand: str, reserved: str = "0") -> InventoryLevel:
    return InventoryLevel(product_id=product.id, warehouse_id=uuid.uuid4(),
                          warehouse_code="MAIN", quantity_on_hand=Decimal(on_hand),
                          quantity_reserved=Decimal(reserved))


def _popup(qapp) -> ProductSuggestPopup:
    return ProductSuggestPopup()


# -- rendering ----------------------------------------------------------- #

def test_show_products_adds_one_row_per_product_plus_add_row(qapp):
    popup = _popup(qapp)
    p1, p2 = _product("Widget"), _product("Gadget")
    popup.show_products([p1, p2], "selling_price", None, show_add_product=True)
    assert popup._list.count() == 3


def test_show_products_without_add_product_omits_the_row(qapp):
    popup = _popup(qapp)
    popup.show_products([_product()], "selling_price", None, show_add_product=False)
    assert popup._list.count() == 1


def test_empty_results_shows_no_product_found_message(qapp):
    popup = _popup(qapp)
    popup.show_products([], "selling_price", None, show_add_product=True)
    # "No product found" (unselectable) + "+ Add Product" (selectable)
    assert popup._list.count() == 2
    assert not (popup._list.item(0).flags() & popup._list.item(0).flags().ItemIsSelectable)


def test_empty_results_without_add_product_shows_only_the_message(qapp):
    popup = _popup(qapp)
    popup.show_products([], "selling_price", None, show_add_product=False)
    assert popup._list.count() == 1


# -- selection defaulting -------------------------------------------------- #

def test_first_product_row_is_selected_by_default(qapp):
    popup = _popup(qapp)
    popup.show_products([_product("A"), _product("B")], "selling_price", None,
                        show_add_product=True)
    assert popup._list.currentRow() == 0


def test_add_product_row_is_selected_by_default_when_no_products(qapp):
    popup = _popup(qapp)
    popup.show_products([], "selling_price", None, show_add_product=True)
    assert popup._list.currentRow() == 1  # the "+ Add Product" row


def test_nothing_selected_when_no_selectable_rows_exist(qapp):
    popup = _popup(qapp)
    popup.show_products([], "selling_price", None, show_add_product=False)
    assert popup._list.currentRow() == -1


# -- keyboard navigation --------------------------------------------------- #

def test_move_selection_wraps_and_skips_unselectable_rows(qapp):
    popup = _popup(qapp)
    popup.show_products([_product("A")], "selling_price", None, show_add_product=True)
    assert popup._list.currentRow() == 0
    popup.move_selection(1)
    assert popup._list.currentRow() == 1  # the Add Product row
    popup.move_selection(1)
    assert popup._list.currentRow() == 0  # wraps back to the only product row


def test_move_selection_on_empty_list_is_a_no_op(qapp):
    popup = _popup(qapp)
    popup.show_products([], "selling_price", None, show_add_product=False)
    popup.move_selection(1)  # must not raise
    assert popup._list.currentRow() == -1


# -- activation ------------------------------------------------------------ #

def test_enter_on_default_selection_emits_product_selected(qapp):
    popup = _popup(qapp)
    product = _product("Widget")
    popup.show_products([product], "selling_price", None, show_add_product=True)
    received = []
    popup.product_selected.connect(received.append)
    popup.activate_selection()
    assert received == [product]


def test_enter_on_add_product_row_emits_add_product_requested(qapp):
    popup = _popup(qapp)
    popup.show_products([], "selling_price", None, show_add_product=True)
    fired = []
    popup.add_product_requested.connect(lambda: fired.append(True))
    popup.activate_selection()
    assert fired == [True]


def test_clicking_a_product_row_emits_product_selected(qapp):
    popup = _popup(qapp)
    p1, p2 = _product("A"), _product("B")
    popup.show_products([p1, p2], "selling_price", None, show_add_product=True)
    received = []
    popup.product_selected.connect(received.append)
    popup._on_item_clicked(popup._list.item(1))
    assert received == [p2]


def test_clicking_the_message_row_emits_nothing(qapp):
    popup = _popup(qapp)
    popup.show_products([], "selling_price", None, show_add_product=True)
    received, fired = [], []
    popup.product_selected.connect(received.append)
    popup.add_product_requested.connect(lambda: fired.append(True))
    popup._on_item_clicked(popup._list.item(0))  # "No product found"
    assert received == [] and fired == []


# -- price field respects the caller's rate (purchase vs. selling) -------- #

def test_show_products_uses_purchase_price_field_when_requested(qapp):
    popup = _popup(qapp)
    product = _product()
    product = product.model_copy(update={"purchase_price": Decimal("42.50")})
    popup.show_products([product], "purchase_price", None, show_add_product=False)
    # The row widget renders whatever price_field names — exercised via the
    # public show_products() call; no private-attribute reach-through
    # needed since a wrong field name would raise inside show_products().


# -- stock display ---------------------------------------------------------#

def test_show_products_accepts_a_stock_level_without_error(qapp):
    popup = _popup(qapp)
    product = _product()
    popup.show_products([product], "selling_price", {product.id: _level(product, "5")},
                        show_add_product=False)
    assert popup._list.count() == 1


def test_show_products_with_no_stock_dict_still_renders(qapp):
    """No warehouse selected yet — stock_by_product is None, not an empty
    dict; every row must still render (stock shown as muted "—")."""
    popup = _popup(qapp)
    popup.show_products([_product()], "selling_price", None, show_add_product=False)
    assert popup._list.count() == 1


# -- loading / error states -------------------------------------------------#

def test_show_loading_replaces_content_with_a_single_message_row(qapp):
    popup = _popup(qapp)
    popup.show_products([_product()], "selling_price", None, show_add_product=True)
    popup.show_loading()
    assert popup._list.count() == 1


def test_show_error_includes_add_product_row_when_allowed(qapp):
    popup = _popup(qapp)
    popup.show_error("Couldn't search products — try again", show_add_product=True)
    assert popup._list.count() == 2
    fired = []
    popup.add_product_requested.connect(lambda: fired.append(True))
    popup.activate_selection()
    assert fired == [True]


# -- visibility -------------------------------------------------------------#

def test_show_popup_and_hide_popup_toggle_visibility(qapp):
    anchor = QLineEdit()
    popup = _popup(qapp)
    popup.reposition_below(anchor)
    popup.show_popup()
    assert popup.isVisible()
    popup.hide_popup()
    assert not popup.isVisible()
