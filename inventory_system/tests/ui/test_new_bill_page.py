"""NewBillPage — the "+ Add Product" integration added for smart product
selection: permission-gated visibility, the catalog-options fetch that
feeds AddProductDialog, the not-yet-loaded guard, and — the core data-
safety requirement — that opening/using Add Product never disturbs any
other field already on the bill.

Reference-data loading (customers/warehouses) and the Save Draft/Finalize
flows are pre-existing behavior this feature must not change; they're not
re-tested here.
"""
import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

try:
    from PySide6.QtWidgets import QApplication, QMessageBox
except ImportError:
    pytest.skip("PySide6 not available", allow_module_level=True)

import app.ui.pages.new_bill_page as new_bill_page
from app.schemas.product import ProductPage
from app.security.session import SessionManager
from app.ui.pages.new_bill_page import NewBillPage


@pytest.fixture(scope="module")
def qapp():
    try:
        return QApplication.instance() or QApplication([])
    except Exception as exc:  # noqa: BLE001 - e.g. no display available
        pytest.skip(f"cannot create QApplication: {exc}")


def _sessions(permissions):
    sessions = SessionManager(idle_timeout=timedelta(minutes=30))
    sessions.start(user_id=uuid.uuid4(), organization_id=uuid.uuid4(), role_id=uuid.uuid4(),
                   permissions=frozenset(permissions), is_superuser=False,
                   must_change_password=False,
                   now=__import__("datetime").datetime.now(__import__("datetime").timezone.utc))
    return sessions


def _services():
    sales_service = MagicMock()
    sales_service.list_customers.return_value = []
    inventory_service = MagicMock()
    inventory_service.list_warehouses.return_value = []
    product_service = MagicMock()
    product_service.search_products.return_value = ProductPage(items=[], total=0, page=1,
                                                                page_size=500)
    return sales_service, inventory_service, product_service


def _page(qapp, *, permissions=(), catalog_service=None, organization_service=None):
    sales_service, inventory_service, product_service = _services()
    return NewBillPage(sales_service, inventory_service, product_service,
                       _sessions(permissions), catalog_service, organization_service)


# -- permission gating --------------------------------------------------------#

def test_add_product_creation_disabled_without_permission(qapp):
    page = _page(qapp, permissions=frozenset())
    assert page._items_table._allow_product_creation is False


def test_add_product_creation_enabled_with_permission(qapp):
    page = _page(qapp, permissions={"products.create"}, catalog_service=MagicMock())
    assert page._items_table._allow_product_creation is True


def test_catalog_not_loaded_without_products_create_permission(qapp):
    catalog_service = MagicMock()
    page = _page(qapp, permissions=frozenset(), catalog_service=catalog_service)
    catalog_service.list_categories.assert_not_called()


# -- _fetch_catalog_options ---------------------------------------------------#

def test_fetch_catalog_options_returns_org_default_tax_percent(qapp):
    page = _page(qapp, permissions={"products.create"})
    page._catalog_service = MagicMock()
    page._catalog_service.list_categories.return_value = ["cat"]
    page._catalog_service.list_brands.return_value = ["brand"]
    page._catalog_service.list_units.return_value = ["unit"]
    page._organization_service = MagicMock()
    page._organization_service.get_current_organization.return_value = MagicMock(
        default_tax_percent=Decimal("13"))

    categories, brands, units, default_tax_percent = page._fetch_catalog_options()

    assert categories == ["cat"] and brands == ["brand"] and units == ["unit"]
    assert default_tax_percent == Decimal("13")


def test_fetch_catalog_options_defaults_tax_percent_without_organization_service(qapp):
    page = _page(qapp, permissions={"products.create"})
    page._catalog_service = MagicMock()
    page._catalog_service.list_categories.return_value = []
    page._catalog_service.list_brands.return_value = []
    page._catalog_service.list_units.return_value = []
    page._organization_service = None

    *_rest, default_tax_percent = page._fetch_catalog_options()

    assert default_tax_percent == Decimal("0")


# -- _on_add_product_requested -------------------------------------------------#

def test_add_product_requested_shows_message_when_catalog_still_loading(qapp, monkeypatch):
    page = _page(qapp, permissions={"products.create"}, catalog_service=MagicMock())
    assert page._catalog_loaded is False  # nothing has completed the async load yet

    called = []
    monkeypatch.setattr(QMessageBox, "information",
                        lambda *a, **k: called.append(True))
    monkeypatch.setattr(new_bill_page, "AddProductDialog",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("dialog must not open before catalog loads")))

    page._on_add_product_requested("Typed Product Name")

    assert called == [True]


def test_add_product_requested_prefills_dialog_with_typed_text(qapp, monkeypatch):
    page = _page(qapp, permissions={"products.create"}, catalog_service=MagicMock())
    page._catalog_loaded = True
    captured = {}

    class _FakeDialog:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)
            self.product = None

        def exec(self):
            return False  # user cancelled

    monkeypatch.setattr(new_bill_page, "AddProductDialog", _FakeDialog)
    page._on_add_product_requested("Typed Product Name")

    assert captured["initial_name"] == "Typed Product Name"


def test_cancelling_add_product_dialog_adds_no_row(qapp, monkeypatch):
    page = _page(qapp, permissions={"products.create"}, catalog_service=MagicMock())
    page._catalog_loaded = True

    class _FakeDialog:
        def __init__(self, *a, **k):
            self.product = None

        def exec(self):
            return False

    monkeypatch.setattr(new_bill_page, "AddProductDialog", _FakeDialog)
    page._on_add_product_requested("Anything")

    assert page._items_table._table.rowCount() == 0


# -- data safety: everything else on the bill survives Add Product -----------#

def test_add_product_success_adds_exactly_one_row_and_preserves_other_fields(qapp, monkeypatch):
    page = _page(qapp, permissions={"products.create"}, catalog_service=MagicMock())
    page._catalog_loaded = True

    # Fill in unrelated bill state the way a real user would before
    # reaching for "+ Add Product" mid-bill.
    page._notes_edit.setPlainText("Deliver by Friday, call ahead")
    page._reference_number_edit.setText("PO-998877")
    page._due_date_check.setChecked(True)
    page._overall_discount_edit.setText("25")

    from datetime import datetime, timezone

    from app.domain.product import ProductStatus, ProductType
    from app.schemas.product import ProductOut, UnitOut

    now = datetime.now(timezone.utc)
    new_product = ProductOut(
        id=uuid.uuid4(), sku="NEW-SKU", barcode=None, name="Brand New Product",
        description=None, product_type=ProductType.GOODS, category=None, brand=None,
        unit=UnitOut(id=uuid.uuid4(), name="Piece", abbreviation="pc"), sub_unit=None,
        sub_unit_conversion_factor=None, tertiary_unit=None,
        tertiary_unit_conversion_factor=None, purchase_price=Decimal("10"),
        selling_price=Decimal("25"), tax_percent=Decimal("13"), is_taxable=True,
        excise_percent=Decimal("0"), minimum_stock_level=Decimal("0"), hsn_code=None,
        size=None, color=None, flavour=None, dftqc_no=None, country_of_origin=None,
        expiry_date=None, status=ProductStatus.ACTIVE, created_at=now, updated_at=now)

    class _FakeDialog:
        def __init__(self, *a, **k):
            self.product = new_product

        def exec(self):
            return True  # user saved

    monkeypatch.setattr(new_bill_page, "AddProductDialog", _FakeDialog)
    page._on_add_product_requested("Brand New Product")

    assert page._items_table._table.rowCount() == 1
    assert page._items_table._table.item(0, 1).text() == "Brand New Product"
    # Nothing else on the bill was touched.
    assert page._notes_edit.toPlainText() == "Deliver by Friday, call ahead"
    assert page._reference_number_edit.text() == "PO-998877"
    assert page._due_date_check.isChecked() is True
    assert page._overall_discount_edit.text() == "25"


def test_add_product_success_does_not_disturb_existing_items(qapp, monkeypatch):
    """Adding a second product via Add Product must not touch a row
    already present in the bill from a normal search-and-select."""
    page = _page(qapp, permissions={"products.create"}, catalog_service=MagicMock())
    page._catalog_loaded = True

    from datetime import datetime, timezone

    from app.domain.product import ProductStatus, ProductType
    from app.schemas.product import ProductOut, UnitOut

    def _product(name, sku):
        now = datetime.now(timezone.utc)
        return ProductOut(
            id=uuid.uuid4(), sku=sku, barcode=None, name=name, description=None,
            product_type=ProductType.GOODS, category=None, brand=None,
            unit=UnitOut(id=uuid.uuid4(), name="Piece", abbreviation="pc"), sub_unit=None,
            sub_unit_conversion_factor=None, tertiary_unit=None,
            tertiary_unit_conversion_factor=None, purchase_price=Decimal("10"),
            selling_price=Decimal("25"), tax_percent=Decimal("13"), is_taxable=True,
            excise_percent=Decimal("0"), minimum_stock_level=Decimal("0"), hsn_code=None,
            size=None, color=None, flavour=None, dftqc_no=None, country_of_origin=None,
            expiry_date=None, status=ProductStatus.ACTIVE, created_at=now, updated_at=now)

    existing = _product("Existing Product", "EXIST-1")
    page._items_table.add_row(existing, quantity=Decimal("3"))

    new_product = _product("Brand New Product", "NEW-SKU")

    class _FakeDialog:
        def __init__(self, *a, **k):
            self.product = new_product

        def exec(self):
            return True

    monkeypatch.setattr(new_bill_page, "AddProductDialog", _FakeDialog)
    page._on_add_product_requested("Brand New Product")

    assert page._items_table._table.rowCount() == 2
    assert page._items_table._table.item(0, 1).text() == "Existing Product"
    assert page._items_table._table.cellWidget(0, 4).text() == "3"  # quantity untouched
    assert page._items_table._table.item(1, 1).text() == "Brand New Product"
