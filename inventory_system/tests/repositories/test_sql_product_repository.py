"""SqlProductRepository (and SqlCategoryRepository/SqlBrandRepository/
SqlUnitRepository) against a live PostgreSQL database — proves search,
sort, pagination, and the unique/partial-unique constraints actually work
against real SQL, not just the fake-repository assumptions in
tests/services/test_product_service.py.

Uses the ``live_db`` fixture (tests/conftest.py) — see its docstring for
why this is gated on INVENTORY_TEST_DATABASE_URL, separate from the app's
real INVENTORY_DATABASE_URL, and how to run these locally.
"""
import uuid
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.exceptions import DuplicateBarcodeError, DuplicateSkuError, WarehouseNotFoundError
from app.database.session import get_session
from app.domain.product import ProductStatus
from app.models import Inventory, InventoryTransaction, Organization, Unit, Warehouse
from app.repositories.sql.product_repository import SqlProductRepository
from app.repositories.sql.unit_repository import SqlUnitRepository
from app.schemas.product import ProductCreate, ProductFilter


@pytest.fixture()
def org_and_unit(live_db):
    with get_session() as session:
        org = Organization(name="Acme Traders")
        session.add(org)
        session.flush()
        unit = Unit(organization_id=org.id, name="Piece", abbreviation="pc")
        session.add(unit)
        session.flush()
        return org.id, unit.id


def _product(sku, unit_id, **overrides):
    kwargs = dict(sku=sku, barcode=None, name=f"Product {sku}", unit_id=unit_id,
                  purchase_price=Decimal("10.00"), selling_price=Decimal("15.00"),
                  tax_percent=Decimal("13.00"), minimum_stock_level=Decimal("0"))
    kwargs.update(overrides)
    return ProductCreate(**kwargs)


def test_create_then_get_by_id_round_trips(org_and_unit):
    org_id, unit_id = org_and_unit
    repo = SqlProductRepository()
    created = repo.create(org_id, _product("SKU-1", unit_id, name="Widget"))

    fetched = repo.get_by_id(org_id, created.id)

    assert fetched.name == "Widget"
    assert fetched.unit.abbreviation == "pc"
    assert fetched.status == ProductStatus.ACTIVE
    assert isinstance(fetched.purchase_price, Decimal)


def test_create_persists_excise_percent(org_and_unit):
    org_id, unit_id = org_and_unit
    repo = SqlProductRepository()
    created = repo.create(org_id, _product("SKU-1", unit_id, excise_percent=Decimal("6.5")))

    fetched = repo.get_by_id(org_id, created.id)
    assert fetched.excise_percent == Decimal("6.5")


def test_update_persists_excise_percent(org_and_unit):
    from app.schemas.product import ProductUpdate

    org_id, unit_id = org_and_unit
    repo = SqlProductRepository()
    created = repo.create(org_id, _product("SKU-1", unit_id, excise_percent=Decimal("0")))

    updated = repo.update(org_id, created.id, ProductUpdate(excise_percent=Decimal("9.25")))
    assert updated.excise_percent == Decimal("9.25")


def test_duplicate_sku_within_same_org_rejected(org_and_unit):
    org_id, unit_id = org_and_unit
    repo = SqlProductRepository()
    repo.create(org_id, _product("SKU-1", unit_id))
    with pytest.raises(DuplicateSkuError):
        repo.create(org_id, _product("SKU-1", unit_id))


def test_same_sku_allowed_across_different_organizations(live_db):
    with get_session() as session:
        org_a = Organization(name="Org A")
        org_b = Organization(name="Org B")
        session.add_all([org_a, org_b])
        session.flush()
        unit_a = Unit(organization_id=org_a.id, name="Piece", abbreviation="pc")
        unit_b = Unit(organization_id=org_b.id, name="Piece", abbreviation="pc")
        session.add_all([unit_a, unit_b])
        session.flush()
        org_a_id, org_b_id, unit_a_id, unit_b_id = org_a.id, org_b.id, unit_a.id, unit_b.id

    repo = SqlProductRepository()
    repo.create(org_a_id, _product("SHARED-SKU", unit_a_id))
    repo.create(org_b_id, _product("SHARED-SKU", unit_b_id))  # must not raise


def test_barcode_unique_only_when_present(org_and_unit):
    org_id, unit_id = org_and_unit
    repo = SqlProductRepository()
    repo.create(org_id, _product("SKU-1", unit_id, barcode=None))
    repo.create(org_id, _product("SKU-2", unit_id, barcode=None))  # two NULLs: fine
    repo.create(org_id, _product("SKU-3", unit_id, barcode="111"))
    with pytest.raises(DuplicateBarcodeError):
        repo.create(org_id, _product("SKU-4", unit_id, barcode="111"))


def test_check_constraint_rejects_negative_selling_price(org_and_unit):
    org_id, unit_id = org_and_unit
    repo = SqlProductRepository()
    with pytest.raises(IntegrityError):
        repo.create(org_id, _product("SKU-1", unit_id, selling_price=Decimal("-1")))


def test_sku_exists_and_barcode_exists(org_and_unit):
    org_id, unit_id = org_and_unit
    repo = SqlProductRepository()
    created = repo.create(org_id, _product("SKU-1", unit_id, barcode="999"))

    assert repo.sku_exists(org_id, "SKU-1") is True
    assert repo.sku_exists(org_id, "SKU-1", exclude_id=created.id) is False
    assert repo.barcode_exists(org_id, "999") is True
    assert repo.barcode_exists(org_id, "999", exclude_id=created.id) is False


def test_search_filters_by_status(org_and_unit):
    org_id, unit_id = org_and_unit
    repo = SqlProductRepository()
    active = repo.create(org_id, _product("SKU-1", unit_id))
    archived = repo.create(org_id, _product("SKU-2", unit_id))
    repo.set_status(org_id, archived.id, ProductStatus.ARCHIVED)

    active_page = repo.search(org_id, ProductFilter(status=ProductStatus.ACTIVE))
    assert [p.id for p in active_page.items] == [active.id]

    archived_page = repo.search(org_id, ProductFilter(status=ProductStatus.ARCHIVED))
    assert [p.id for p in archived_page.items] == [archived.id]


def test_search_matches_sku_barcode_or_name(org_and_unit):
    org_id, unit_id = org_and_unit
    repo = SqlProductRepository()
    repo.create(org_id, _product("ABC-1", unit_id, name="Blue Widget", barcode="1111"))
    repo.create(org_id, _product("XYZ-9", unit_id, name="Red Gadget", barcode="2222"))

    by_name = repo.search(org_id, ProductFilter(search="widget"))
    assert len(by_name.items) == 1 and by_name.items[0].sku == "ABC-1"

    by_barcode = repo.search(org_id, ProductFilter(search="2222"))
    assert len(by_barcode.items) == 1 and by_barcode.items[0].sku == "XYZ-9"


def test_search_matches_hsn_code(org_and_unit):
    org_id, unit_id = org_and_unit
    repo = SqlProductRepository()
    repo.create(org_id, _product("ABC-1", unit_id, name="Blue Widget", hsn_code="8471"))
    repo.create(org_id, _product("XYZ-9", unit_id, name="Red Gadget", hsn_code="3004"))

    by_hsn = repo.search(org_id, ProductFilter(search="8471"))
    assert len(by_hsn.items) == 1 and by_hsn.items[0].sku == "ABC-1"


def test_search_with_no_hsn_code_does_not_match_stray_terms(org_and_unit):
    # A product with hsn_code=None must not accidentally match every search
    # (ILIKE against NULL is NULL/false, never true) — this pins that.
    org_id, unit_id = org_and_unit
    repo = SqlProductRepository()
    repo.create(org_id, _product("ABC-1", unit_id, name="Blue Widget", hsn_code=None))

    result = repo.search(org_id, ProductFilter(search="8471"))
    assert result.items == []


def test_search_pagination(org_and_unit):
    org_id, unit_id = org_and_unit
    repo = SqlProductRepository()
    for i in range(5):
        repo.create(org_id, _product(f"SKU-{i}", unit_id, name=f"Product {i}"))

    page1 = repo.search(org_id, ProductFilter(page=1, page_size=2, sort_by="sku"))
    page2 = repo.search(org_id, ProductFilter(page=2, page_size=2, sort_by="sku"))
    page3 = repo.search(org_id, ProductFilter(page=3, page_size=2, sort_by="sku"))

    assert page1.total == 5
    assert page1.total_pages == 3
    assert [p.sku for p in page1.items] == ["SKU-0", "SKU-1"]
    assert [p.sku for p in page2.items] == ["SKU-2", "SKU-3"]
    assert [p.sku for p in page3.items] == ["SKU-4"]


def test_search_sort_descending(org_and_unit):
    org_id, unit_id = org_and_unit
    repo = SqlProductRepository()
    repo.create(org_id, _product("A", unit_id, purchase_price=Decimal("5")))
    repo.create(org_id, _product("B", unit_id, purchase_price=Decimal("20")))
    repo.create(org_id, _product("C", unit_id, purchase_price=Decimal("10")))

    page = repo.search(org_id, ProductFilter(sort_by="purchase_price", sort_desc=True))
    assert [p.sku for p in page.items] == ["B", "C", "A"]


def test_category_brand_unit_repositories_scope_by_organization(live_db):
    from app.repositories.sql.brand_repository import SqlBrandRepository
    from app.repositories.sql.category_repository import SqlCategoryRepository
    from app.schemas.product import BrandCreate, CategoryCreate

    with get_session() as session:
        org_a = Organization(name="Org A")
        org_b = Organization(name="Org B")
        session.add_all([org_a, org_b])
        session.flush()
        org_a_id, org_b_id = org_a.id, org_b.id

    categories = SqlCategoryRepository()
    categories.create(org_a_id, CategoryCreate(name="Beverages"))
    categories.create(org_b_id, CategoryCreate(name="Snacks"))

    assert [c.name for c in categories.list_all(org_a_id)] == ["Beverages"]
    assert [c.name for c in categories.list_all(org_b_id)] == ["Snacks"]

    brands = SqlBrandRepository()
    created = brands.create(org_a_id, BrandCreate(name="Acme"))
    assert brands.get_by_id(org_b_id, created.id) is None  # wrong org, not found
    assert brands.get_by_id(org_a_id, created.id) is not None


# -- create_with_opening_stock: the atomic Add Product + opening stock flow -#

@pytest.fixture()
def org_unit_warehouse(live_db):
    with get_session() as session:
        org = Organization(name="Acme Traders")
        session.add(org)
        session.flush()
        unit = Unit(organization_id=org.id, name="Piece", abbreviation="pc")
        warehouse = Warehouse(organization_id=org.id, code="MAIN", name="Main")
        session.add_all([unit, warehouse])
        session.flush()
        return org.id, unit.id, warehouse.id


def test_create_with_opening_stock_creates_product_and_inventory_atomically(
        org_unit_warehouse):
    org_id, unit_id, warehouse_id = org_unit_warehouse
    repo = SqlProductRepository()
    performed_by = _seed_user(org_id)

    product, transaction = repo.create_with_opening_stock(
        org_id, _product("SKU-STOCK-1", unit_id), warehouse_id, Decimal("30"), performed_by)

    assert transaction is not None
    assert transaction.quantity_change == Decimal("30")
    assert transaction.quantity_on_hand_after == Decimal("30")

    with get_session() as session:
        inv = (session.query(Inventory)
              .filter_by(product_id=product.id, warehouse_id=warehouse_id).one())
        assert inv.quantity_on_hand == Decimal("30")
        tx_rows = (session.query(InventoryTransaction)
                  .filter_by(product_id=product.id).all())
        assert len(tx_rows) == 1
        assert tx_rows[0].reference_type is None  # not tied to a purchase/sale, just opening


def test_create_with_opening_stock_zero_quantity_creates_no_inventory_row(org_unit_warehouse):
    org_id, unit_id, warehouse_id = org_unit_warehouse
    repo = SqlProductRepository()
    performed_by = _seed_user(org_id)

    product, transaction = repo.create_with_opening_stock(
        org_id, _product("SKU-STOCK-2", unit_id), None, Decimal("0"), performed_by)

    assert transaction is None
    with get_session() as session:
        assert session.query(Inventory).filter_by(product_id=product.id).count() == 0


def test_create_with_opening_stock_rolls_back_product_when_warehouse_invalid(
        org_unit_warehouse):
    """The core "if anything fails: ROLLBACK EVERYTHING" guarantee — an
    invalid warehouse must not leave behind a product with no stock record,
    or any other partial state.
    """
    org_id, unit_id, warehouse_id = org_unit_warehouse
    repo = SqlProductRepository()
    performed_by = _seed_user(org_id)
    fake_warehouse_id = uuid.uuid4()

    with pytest.raises(WarehouseNotFoundError):
        repo.create_with_opening_stock(org_id, _product("SKU-STOCK-3", unit_id),
                                       fake_warehouse_id, Decimal("10"), performed_by)

    with get_session() as session:
        assert session.query(Inventory).count() == 0
        from app.models import Product
        assert session.query(Product).filter_by(sku="SKU-STOCK-3").count() == 0, (
            "product must not exist when its opening-stock write failed — "
            "the whole transaction must have rolled back")


def test_create_with_opening_stock_rejects_warehouse_from_another_organization(
        org_unit_warehouse):
    org_id, unit_id, warehouse_id = org_unit_warehouse
    with get_session() as session:
        other_org = Organization(name="Other Org")
        session.add(other_org)
        session.flush()
        other_warehouse = Warehouse(organization_id=other_org.id, code="OTHER", name="Other")
        session.add(other_warehouse)
        session.flush()
        other_warehouse_id = other_warehouse.id

    repo = SqlProductRepository()
    performed_by = _seed_user(org_id)
    with pytest.raises(WarehouseNotFoundError):
        repo.create_with_opening_stock(org_id, _product("SKU-STOCK-4", unit_id),
                                       other_warehouse_id, Decimal("10"), performed_by)

    with get_session() as session:
        from app.models import Product
        assert session.query(Product).filter_by(sku="SKU-STOCK-4").count() == 0


def test_create_with_opening_stock_rejects_duplicate_sku_with_no_partial_state(
        org_unit_warehouse):
    org_id, unit_id, warehouse_id = org_unit_warehouse
    repo = SqlProductRepository()
    performed_by = _seed_user(org_id)
    repo.create(org_id, _product("SKU-DUP", unit_id))

    with pytest.raises(DuplicateSkuError):
        repo.create_with_opening_stock(org_id, _product("SKU-DUP", unit_id), warehouse_id,
                                       Decimal("10"), performed_by)

    with get_session() as session:
        assert session.query(Inventory).count() == 0


def _seed_user(org_id):
    from app.models import User
    with get_session() as session:
        user = User(email=f"opener-{org_id}@example.com", username=f"opener{str(org_id)[:8]}",
                   hashed_password="x", full_name="Opener")
        session.add(user)
        session.flush()
        return user.id
