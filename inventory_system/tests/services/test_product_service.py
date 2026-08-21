"""ProductService tested against hand-written fake repositories — no
database. Proves validation/normalization/duplicate-detection happen in the
service (not the repository or a widget), that product.* permissions are
enforced by calling the service directly, exactly as a UI bypass would, and
that opening-stock creation (product + warehouse + inventory.adjust) is
validated correctly before ever reaching the repository.
"""
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.core.exceptions import (
    DuplicateBarcodeError,
    DuplicateSkuError,
    ProductNotFoundError,
    ProductValidationError,
    UnitNotFoundError,
    WarehouseNotFoundError,
)
from app.domain.product import ProductStatus, ProductType
from app.schemas.inventory import InventoryTransactionOut, InventoryTransactionType, WarehouseOut
from app.schemas.product import ProductCreate, ProductFilter, ProductOut, ProductUpdate, UnitOut
from app.security.authorization import PermissionDeniedError
from app.security.session import SessionManager
from app.services.product_service import ProductService

UNIT_ID = uuid.uuid4()
UNIT = UnitOut(id=UNIT_ID, name="Piece", abbreviation="pc")
SUB_UNIT_ID = uuid.uuid4()
SUB_UNIT = UnitOut(id=SUB_UNIT_ID, name="Box", abbreviation="bx")
WAREHOUSE_ID = uuid.uuid4()
ORG_ID = uuid.uuid4()
OTHER_ORG_WAREHOUSE_ID = uuid.uuid4()


class FakeProductRepository:
    def __init__(self):
        self.products: dict[uuid.UUID, ProductOut] = {}

    def _build(self, data: ProductCreate) -> ProductOut:
        from datetime import datetime, timezone
        units_by_id = {UNIT_ID: UNIT, SUB_UNIT_ID: SUB_UNIT}
        return ProductOut(
            id=uuid.uuid4(), sku=data.sku, barcode=data.barcode, name=data.name,
            description=data.description, product_type=data.product_type, category=None,
            brand=None, unit=units_by_id.get(data.unit_id, UNIT),
            sub_unit=units_by_id.get(data.sub_unit_id) if data.sub_unit_id else None,
            sub_unit_conversion_factor=data.sub_unit_conversion_factor,
            tertiary_unit=units_by_id.get(data.tertiary_unit_id) if data.tertiary_unit_id
                else None,
            tertiary_unit_conversion_factor=data.tertiary_unit_conversion_factor,
            purchase_price=data.purchase_price, selling_price=data.selling_price,
            tax_percent=data.tax_percent, is_taxable=data.is_taxable,
            excise_percent=data.excise_percent,
            minimum_stock_level=data.minimum_stock_level, hsn_code=data.hsn_code,
            size=data.size, color=data.color, flavour=data.flavour, dftqc_no=data.dftqc_no,
            country_of_origin=data.country_of_origin, expiry_date=data.expiry_date,
            status=ProductStatus.ACTIVE, created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc))

    def create(self, organization_id, data: ProductCreate) -> ProductOut:
        product = self._build(data)
        self.products[product.id] = product
        return product

    def create_with_opening_stock(self, organization_id, data: ProductCreate, warehouse_id,
                                  opening_quantity, performed_by):
        product = self._build(data)
        self.products[product.id] = product
        transaction = None
        if opening_quantity > 0:
            transaction = InventoryTransactionOut(
                id=uuid.uuid4(), product_id=product.id, warehouse_id=warehouse_id,
                transaction_type=InventoryTransactionType.STOCK_IN,
                quantity_change=opening_quantity, quantity_on_hand_after=opening_quantity,
                quantity_reserved_after=Decimal("0"), reference_type=None, reference_id=None,
                performed_by=performed_by, notes="Opening stock (Add Product)",
                created_at=product.created_at)
        return product, transaction

    def update(self, organization_id, product_id, data: ProductUpdate):
        existing = self.products.get(product_id)
        if existing is None:
            return None
        updated = existing.model_copy(update=data.model_dump(exclude_unset=True))
        self.products[product_id] = updated
        return updated

    def get_by_id(self, organization_id, product_id):
        return self.products.get(product_id)

    def sku_exists(self, organization_id, sku, exclude_id=None):
        return any(p.sku == sku and p.id != exclude_id for p in self.products.values())

    def barcode_exists(self, organization_id, barcode, exclude_id=None):
        return any(p.barcode == barcode and p.id != exclude_id
                  for p in self.products.values())

    def set_status(self, organization_id, product_id, status):
        if product_id in self.products:
            self.products[product_id] = self.products[product_id].model_copy(
                update={"status": status})

    def search(self, organization_id, filter):
        raise NotImplementedError  # not exercised by these tests


class FakeWarehouseRepository:
    def __init__(self):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        self.warehouses = {
            WAREHOUSE_ID: WarehouseOut(id=WAREHOUSE_ID, code="MAIN", name="Main", address=None,
                                       is_active=True, created_at=now, updated_at=now),
        }

    def get_by_id(self, organization_id, warehouse_id):
        if warehouse_id == OTHER_ORG_WAREHOUSE_ID:
            return None  # simulates a warehouse belonging to a different org
        return self.warehouses.get(warehouse_id)


class FakeUnitRepository:
    def __init__(self):
        self.units = {UNIT_ID: UNIT, SUB_UNIT_ID: SUB_UNIT}

    def get_by_id(self, organization_id, unit_id):
        return self.units.get(unit_id)


class FakeAuditLogRepository:
    def __init__(self):
        self.entries: list[dict] = []

    def record(self, *, organization_id, user_id, actor_email, organization_name, action,
              entity_type=None, entity_id=None, changes=None):
        self.entries.append({"organization_id": organization_id, "user_id": user_id,
                            "actor_email": actor_email, "organization_name": organization_name,
                            "action": action, "entity_type": entity_type,
                            "entity_id": entity_id, "changes": changes})


def _service(permissions=frozenset({"products.create", "products.view", "products.update",
                                    "products.delete", "inventory.adjust"}),
            repo=None, audit_log=None, warehouses=None, units=None):
    from datetime import datetime, timezone
    repo = repo or FakeProductRepository()
    audit_log = audit_log if audit_log is not None else FakeAuditLogRepository()
    warehouses = warehouses if warehouses is not None else FakeWarehouseRepository()
    units = units if units is not None else FakeUnitRepository()
    sessions = SessionManager(idle_timeout=timedelta(minutes=30))
    sessions.start(user_id=uuid.uuid4(), organization_id=ORG_ID, role_id=uuid.uuid4(),
                   permissions=frozenset(permissions), is_superuser=False,
                   must_change_password=False, now=datetime.now(timezone.utc))
    return ProductService(repo, sessions, audit_log, warehouses, units), repo


def _create_data(**overrides):
    kwargs = dict(sku="abc-1", barcode=None, name="Widget", unit_id=UNIT_ID,
                  purchase_price=Decimal("10"), selling_price=Decimal("15"),
                  tax_percent=Decimal("13"), minimum_stock_level=Decimal("5"))
    kwargs.update(overrides)
    return ProductCreate(**kwargs)


def _create(service, **overrides) -> ProductOut:
    product, _transaction = service.create_product(_create_data(**overrides))
    return product


def test_create_product_normalizes_sku_to_uppercase():
    service, repo = _service()
    created = _create(service, sku="  abc-1  ")
    assert created.sku == "ABC-1"


def test_create_product_normalizes_blank_barcode_to_none():
    service, _ = _service()
    created = _create(service, barcode="   ")
    assert created.barcode is None


def test_create_product_rejects_invalid_data():
    service, repo = _service()
    with pytest.raises(ProductValidationError):
        service.create_product(_create_data(name=""))
    assert repo.products == {}


def test_create_product_rejects_duplicate_sku():
    service, _ = _service()
    _create(service, sku="ABC-1")
    with pytest.raises(DuplicateSkuError):
        service.create_product(_create_data(sku="abc-1"))  # normalizes to same SKU


def test_create_product_rejects_duplicate_barcode():
    service, _ = _service()
    _create(service, sku="ABC-1", barcode="111")
    with pytest.raises(DuplicateBarcodeError):
        service.create_product(_create_data(sku="ABC-2", barcode="111"))


def test_create_product_requires_permission():
    service, repo = _service(permissions=frozenset())
    with pytest.raises(PermissionDeniedError):
        service.create_product(_create_data())
    assert repo.products == {}


def test_create_product_defaults_to_goods_type():
    service, _ = _service()
    created = _create(service)
    assert created.product_type == ProductType.GOODS


def test_create_service_product_does_not_require_warehouse():
    service, _ = _service()
    created = _create(service, sku="SVC-1", product_type=ProductType.SERVICE)
    assert created.product_type == ProductType.SERVICE


# -- opening stock ---------------------------------------------------------#

def test_create_product_with_opening_quantity_creates_inventory_transaction():
    service, repo = _service()
    product, transaction = service.create_product(
        _create_data(sku="STK-1"), warehouse_id=WAREHOUSE_ID, opening_quantity=Decimal("25"))
    assert transaction is not None
    assert transaction.quantity_change == Decimal("25")
    assert transaction.warehouse_id == WAREHOUSE_ID
    assert transaction.product_id == product.id


def test_create_product_with_zero_opening_quantity_creates_no_transaction():
    service, _ = _service()
    product, transaction = service.create_product(
        _create_data(sku="STK-2"), warehouse_id=None, opening_quantity=Decimal("0"))
    assert transaction is None


def test_create_product_rejects_opening_quantity_without_warehouse():
    """Never silently create stock without a valid warehouse."""
    service, repo = _service()
    with pytest.raises(ProductValidationError, match="warehouse"):
        service.create_product(_create_data(sku="STK-3"), warehouse_id=None,
                               opening_quantity=Decimal("10"))
    assert repo.products == {}


def test_create_product_rejects_negative_opening_quantity():
    service, repo = _service()
    with pytest.raises(ProductValidationError, match="negative"):
        service.create_product(_create_data(sku="STK-4"), warehouse_id=WAREHOUSE_ID,
                               opening_quantity=Decimal("-5"))
    assert repo.products == {}


def test_create_product_rejects_unknown_warehouse():
    service, repo = _service()
    with pytest.raises(WarehouseNotFoundError):
        service.create_product(_create_data(sku="STK-5"), warehouse_id=uuid.uuid4(),
                               opening_quantity=Decimal("10"))
    assert repo.products == {}


def test_create_product_rejects_warehouse_from_another_organization():
    service, repo = _service()
    with pytest.raises(WarehouseNotFoundError):
        service.create_product(_create_data(sku="STK-6"), warehouse_id=OTHER_ORG_WAREHOUSE_ID,
                               opening_quantity=Decimal("10"))
    assert repo.products == {}


def test_create_product_rejects_opening_quantity_for_non_goods():
    service, repo = _service()
    with pytest.raises(ProductValidationError, match="[Gg]oods"):
        service.create_product(_create_data(sku="STK-7", product_type=ProductType.SERVICE),
                               warehouse_id=WAREHOUSE_ID, opening_quantity=Decimal("10"))
    assert repo.products == {}


def test_create_product_with_opening_quantity_requires_inventory_adjust_permission():
    service, repo = _service(permissions=frozenset({"products.create"}))
    with pytest.raises(PermissionDeniedError):
        service.create_product(_create_data(sku="STK-8"), warehouse_id=WAREHOUSE_ID,
                               opening_quantity=Decimal("10"))
    assert repo.products == {}


def test_create_product_without_opening_quantity_does_not_require_inventory_adjust():
    service, _ = _service(permissions=frozenset({"products.create"}))
    product, transaction = service.create_product(_create_data(sku="STK-9"))
    assert product is not None
    assert transaction is None


# -- units / conversion -----------------------------------------------------#

def test_create_product_rejects_unknown_unit():
    service, repo = _service()
    with pytest.raises(ProductValidationError, match="Unit"):
        service.create_product(_create_data(sku="UNT-1", unit_id=uuid.uuid4()))
    assert repo.products == {}


def test_create_product_accepts_valid_sub_unit_conversion():
    service, _ = _service()
    product = _create(service, sku="UNT-2", sub_unit_id=SUB_UNIT_ID,
                      sub_unit_conversion_factor=Decimal("12"))
    assert product.sub_unit.id == SUB_UNIT_ID
    assert product.sub_unit_conversion_factor == Decimal("12")


def test_create_product_rejects_sub_unit_without_conversion_factor():
    service, repo = _service()
    with pytest.raises(ProductValidationError, match="[Cc]onversion"):
        service.create_product(_create_data(sku="UNT-3", sub_unit_id=SUB_UNIT_ID))
    assert repo.products == {}


def test_create_product_rejects_conversion_factor_without_sub_unit():
    service, repo = _service()
    with pytest.raises(ProductValidationError, match="[Ss]ub-unit"):
        service.create_product(
            _create_data(sku="UNT-4", sub_unit_conversion_factor=Decimal("12")))
    assert repo.products == {}


def test_create_product_rejects_zero_conversion_factor():
    service, repo = _service()
    with pytest.raises(ProductValidationError, match="greater than zero"):
        service.create_product(_create_data(sku="UNT-5", sub_unit_id=SUB_UNIT_ID,
                                            sub_unit_conversion_factor=Decimal("0")))
    assert repo.products == {}


def test_create_product_rejects_sub_unit_same_as_main_unit():
    service, repo = _service()
    with pytest.raises(ProductValidationError, match="different"):
        service.create_product(_create_data(sku="UNT-6", sub_unit_id=UNIT_ID,
                                            sub_unit_conversion_factor=Decimal("2")))
    assert repo.products == {}


def test_create_product_rejects_unknown_sub_unit():
    service, repo = _service()
    with pytest.raises(ProductValidationError, match="Sub-unit"):
        service.create_product(_create_data(sku="UNT-7", sub_unit_id=uuid.uuid4(),
                                            sub_unit_conversion_factor=Decimal("2")))
    assert repo.products == {}


# -- tax ---------------------------------------------------------------------#

def test_create_product_rejects_non_taxable_with_nonzero_tax_percent():
    service, repo = _service()
    with pytest.raises(ProductValidationError, match="[Tt]ax"):
        service.create_product(_create_data(sku="TAX-1", is_taxable=False,
                                            tax_percent=Decimal("13")))
    assert repo.products == {}


def test_create_product_accepts_non_taxable_with_zero_tax_percent():
    service, _ = _service()
    product = _create(service, sku="TAX-2", is_taxable=False, tax_percent=Decimal("0"))
    assert product.is_taxable is False


# -- expiry ------------------------------------------------------------------#

def test_create_product_rejects_past_expiry_date():
    service, repo = _service()
    with pytest.raises(ProductValidationError, match="[Ee]xpiry"):
        service.create_product(_create_data(sku="EXP-1", expiry_date=date(2000, 1, 1)))
    assert repo.products == {}


def test_create_product_accepts_future_expiry_date():
    service, _ = _service()
    from datetime import timedelta as td
    future = date.today() + td(days=365)
    product = _create(service, sku="EXP-2", expiry_date=future)
    assert product.expiry_date == future


# -- update ------------------------------------------------------------------#

def test_update_product_renormalizes_sku():
    service, _ = _service()
    created = _create(service, sku="ABC-1")
    updated = service.update_product(created.id, ProductUpdate(sku="  xyz-9  "))
    assert updated.sku == "XYZ-9"


def test_update_product_rejects_duplicate_sku_from_another_product():
    service, _ = _service()
    _create(service, sku="ABC-1")
    second = _create(service, sku="ABC-2")
    with pytest.raises(DuplicateSkuError):
        service.update_product(second.id, ProductUpdate(sku="ABC-1"))


def test_update_product_allows_keeping_its_own_sku():
    service, _ = _service()
    created = _create(service, sku="ABC-1")
    updated = service.update_product(created.id, ProductUpdate(sku="ABC-1", name="Renamed"))
    assert updated.name == "Renamed"


def test_update_product_missing_raises_not_found():
    service, _ = _service()
    with pytest.raises(ProductNotFoundError):
        service.update_product(uuid.uuid4(), ProductUpdate(name="X"))


def test_update_product_requires_permission():
    service, repo = _service(permissions={"products.create"})
    created = _create(service)
    other_service, _ = _service(permissions=frozenset(), repo=repo)
    with pytest.raises(PermissionDeniedError):
        other_service.update_product(created.id, ProductUpdate(name="Nope"))


def test_update_product_rejects_unknown_unit():
    service, _ = _service()
    created = _create(service, sku="UPD-1")
    with pytest.raises(ProductValidationError, match="Unit"):
        service.update_product(created.id, ProductUpdate(unit_id=uuid.uuid4()))


# -- archive / restore --------------------------------------------------------#

def test_archive_then_restore_round_trips_status():
    service, _ = _service()
    created = _create(service)
    assert created.status == ProductStatus.ACTIVE

    service.archive_product(created.id)
    assert service.get_product(created.id).status == ProductStatus.ARCHIVED

    service.restore_product(created.id)
    assert service.get_product(created.id).status == ProductStatus.ACTIVE


def test_archive_requires_product_delete_permission():
    service, repo = _service(permissions={"products.create"})
    created = _create(service)
    other_service, _ = _service(permissions=frozenset(), repo=repo)
    with pytest.raises(PermissionDeniedError):
        other_service.archive_product(created.id)


def test_is_available_for_transactions_false_when_archived():
    service, _ = _service()
    created = _create(service)
    assert ProductService.is_available_for_transactions(created) is True
    service.archive_product(created.id)
    archived = service.get_product(created.id)
    assert ProductService.is_available_for_transactions(archived) is False


def test_get_product_missing_raises_not_found():
    service, _ = _service()
    with pytest.raises(ProductNotFoundError):
        service.get_product(uuid.uuid4())
