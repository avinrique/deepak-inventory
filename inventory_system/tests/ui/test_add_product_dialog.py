"""AddProductDialog — initial_name prefill and the duplicate-name warn-but-
allow flow added for New Bill's "+ Add Product". SKU/barcode uniqueness is
already covered by ProductService/SqlProductRepository tests (hard-blocked
server-side, unchanged by this feature); this file is only the new
name-collision courtesy check and its state machine (_name_ack).

The check's *decision* methods (_on_name_check_result/_on_name_check_error)
are called directly with hand-built results rather than driven through a
real QThreadPool Worker — this mirrors the rest of the codebase's
convention of keeping the async-triggering method thin and testing the
callback logic it dispatches to directly.
"""
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

try:
    from PySide6.QtWidgets import QApplication
except ImportError:
    pytest.skip("PySide6 not available", allow_module_level=True)

from app.domain.product import ProductStatus, ProductType
from app.schemas.product import ProductOut, ProductPage, UnitOut
from app.ui.widgets.add_product_dialog import AddProductDialog


@pytest.fixture(scope="module")
def qapp():
    try:
        return QApplication.instance() or QApplication([])
    except Exception as exc:  # noqa: BLE001 - e.g. no display available
        pytest.skip(f"cannot create QApplication: {exc}")


def _product(name="Widget", sku="SKU-1") -> ProductOut:
    now = datetime.now(timezone.utc)
    unit = UnitOut(id=uuid.uuid4(), name="Piece", abbreviation="pc")
    return ProductOut(id=uuid.uuid4(), sku=sku, barcode=None, name=name, description=None,
                      product_type=ProductType.GOODS, category=None, brand=None, unit=unit,
                      sub_unit=None, sub_unit_conversion_factor=None, tertiary_unit=None,
                      tertiary_unit_conversion_factor=None, purchase_price=Decimal("10"),
                      selling_price=Decimal("15"), tax_percent=Decimal("13"), is_taxable=True,
                      excise_percent=Decimal("0"), minimum_stock_level=Decimal("0"),
                      hsn_code=None, size=None, color=None, flavour=None, dftqc_no=None,
                      country_of_origin=None, expiry_date=None, status=ProductStatus.ACTIVE,
                      created_at=now, updated_at=now)


def _dialog(qapp, *, initial_name=None) -> AddProductDialog:
    return AddProductDialog(MagicMock(), categories=[], brands=[], units=[], warehouses=[],
                            initial_name=initial_name)


_DUMMY_DATA = MagicMock()  # ProductCreate stand-in — never inspected by these handlers
_DUMMY_WAREHOUSE_ID = None
_DUMMY_OPENING_QTY = Decimal("0")


# -- initial_name prefill ---------------------------------------------------#

def test_initial_name_prefills_the_name_field(qapp):
    dialog = _dialog(qapp, initial_name="Nonexistent Widget")
    assert dialog._name.text() == "Nonexistent Widget"


def test_no_initial_name_leaves_the_name_field_blank(qapp):
    dialog = _dialog(qapp)
    assert dialog._name.text() == ""


# -- _name_ack state machine -------------------------------------------------#

def test_idle_button_label_is_save_product_before_any_check(qapp):
    dialog = _dialog(qapp)
    assert dialog._idle_button_label() == "Save Product"


def test_editing_the_name_resets_an_armed_acknowledgment(qapp):
    dialog = _dialog(qapp)
    dialog._name_ack = "Widget"
    dialog._duplicate_warning_label.show()

    dialog._name.setText("Widget 2")  # fires _on_name_changed

    assert dialog._name_ack is None
    assert dialog._duplicate_warning_label.isHidden() is True
    assert dialog._save_button.text() == "Save Product"


# -- _on_name_check_result --------------------------------------------------#

def test_no_matching_name_proceeds_straight_to_create(qapp):
    dialog = _dialog(qapp)
    dialog._create_product = MagicMock()
    page = ProductPage(items=[], total=0, page=1, page_size=10)

    dialog._on_name_check_result(page, "Brand New Product", _DUMMY_DATA,
                                 _DUMMY_WAREHOUSE_ID, _DUMMY_OPENING_QTY)

    dialog._create_product.assert_called_once_with(_DUMMY_DATA, _DUMMY_WAREHOUSE_ID,
                                                    _DUMMY_OPENING_QTY)
    assert dialog._name_ack is None
    assert dialog._duplicate_warning_label.isHidden() is True


def test_matching_name_arms_the_warning_instead_of_creating(qapp):
    dialog = _dialog(qapp)
    dialog._create_product = MagicMock()
    existing = _product(name="Widget", sku="WID-001")
    page = ProductPage(items=[existing], total=1, page=1, page_size=10)

    dialog._on_name_check_result(page, "Widget", _DUMMY_DATA, _DUMMY_WAREHOUSE_ID,
                                 _DUMMY_OPENING_QTY)

    dialog._create_product.assert_not_called()
    assert dialog._name_ack == "Widget"
    assert dialog._duplicate_warning_label.isHidden() is False
    assert "WID-001" in dialog._duplicate_warning_label.text()
    assert dialog._save_button.text() == "Create Anyway"


def test_name_match_is_case_insensitive(qapp):
    dialog = _dialog(qapp)
    dialog._create_product = MagicMock()
    existing = _product(name="WIDGET")
    page = ProductPage(items=[existing], total=1, page=1, page_size=10)

    dialog._on_name_check_result(page, "widget", _DUMMY_DATA, _DUMMY_WAREHOUSE_ID,
                                 _DUMMY_OPENING_QTY)

    dialog._create_product.assert_not_called()
    assert dialog._name_ack == "widget"


def test_search_result_with_only_unrelated_names_does_not_arm(qapp):
    """search_products does a substring match, so a search for "Widget"
    can return "Blue Widget Deluxe" — only an EXACT name match should
    count as a duplicate, not every row the search happened to return.
    """
    dialog = _dialog(qapp)
    dialog._create_product = MagicMock()
    page = ProductPage(items=[_product(name="Blue Widget Deluxe")], total=1, page=1,
                       page_size=10)

    dialog._on_name_check_result(page, "Widget", _DUMMY_DATA, _DUMMY_WAREHOUSE_ID,
                                 _DUMMY_OPENING_QTY)

    dialog._create_product.assert_called_once()
    assert dialog._name_ack is None


def test_duplicate_warning_caps_the_listed_matches(qapp):
    dialog = _dialog(qapp)
    dialog._create_product = MagicMock()
    matches = [_product(name="Widget", sku=f"WID-{i:03d}") for i in range(5)]
    page = ProductPage(items=matches, total=5, page=1, page_size=10)

    dialog._on_name_check_result(page, "Widget", _DUMMY_DATA, _DUMMY_WAREHOUSE_ID,
                                 _DUMMY_OPENING_QTY)

    text = dialog._duplicate_warning_label.text()
    assert "WID-000" in text and "WID-002" in text
    assert "WID-004" not in text  # beyond the display limit
    assert "2 more" in text


# -- _on_name_check_error ----------------------------------------------------#

def test_name_check_failure_proceeds_to_create_rather_than_blocking(qapp):
    """The duplicate-name check is a courtesy, not a requirement — if it
    can't run at all, product creation must not be held hostage to it."""
    dialog = _dialog(qapp)
    dialog._create_product = MagicMock()

    dialog._on_name_check_error(RuntimeError("db unavailable"), _DUMMY_DATA,
                                _DUMMY_WAREHOUSE_ID, _DUMMY_OPENING_QTY)

    dialog._create_product.assert_called_once_with(_DUMMY_DATA, _DUMMY_WAREHOUSE_ID,
                                                    _DUMMY_OPENING_QTY)


# -- duplicate warning vs. hard error are mutually exclusive ----------------#

def test_showing_a_hard_error_hides_any_duplicate_warning(qapp):
    dialog = _dialog(qapp)
    dialog._duplicate_warning_label.setText("existing warning")
    dialog._duplicate_warning_label.show()

    dialog._show_error("Something went wrong.")

    assert dialog._duplicate_warning_label.isHidden() is True
    assert dialog._error_label.isHidden() is False
