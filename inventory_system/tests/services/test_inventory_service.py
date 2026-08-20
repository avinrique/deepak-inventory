"""InventoryService tested against hand-written fake repositories — no
database, no real locking. Proves validation, existence checks, and
inventory.*/warehouse.manage permission enforcement happen in the service.
Real row-locking/concurrency guarantees are proven against a live database
in tests/repositories/test_sql_inventory_repository.py — a fake repository
can't exercise those, it's a plain dict.
"""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.core.exceptions import (
    DuplicateWarehouseCodeError,
    InsufficientStockError,
    InventoryValidationError,
    ProductNotFoundError,
    WarehouseNotFoundError,
)
from app.domain.inventory import InventoryTransactionType
from app.schemas.inventory import (
    AdjustmentRequest,
    InventoryLevel,
    InventoryTransactionOut,
    ReturnRequest,
    StockMoveRequest,
    TransactionFilter,
    TransferRequest,
    WarehouseCreate,
    WarehouseOut,
    WarehouseUpdate,
)
from app.schemas.product import ProductOut, UnitOut
from app.security.authorization import PermissionDeniedError
from app.security.session import SessionManager
from app.services.inventory_service import InventoryService

ORG_ID = uuid.uuid4()
UNIT = UnitOut(id=uuid.uuid4(), name="Piece", abbreviation="pc")

ALL_PERMISSIONS = frozenset({"warehouse.manage", "inventory.view", "inventory.adjust",
                             "inventory.transfer"})


class FakeWarehouseRepository:
    def __init__(self):
        self.warehouses: dict[uuid.UUID, WarehouseOut] = {}

    def create(self, organization_id, data: WarehouseCreate) -> WarehouseOut:
        now = datetime.now(timezone.utc)
        warehouse = WarehouseOut(id=uuid.uuid4(), code=data.code, name=data.name,
                                 address=data.address, is_active=True, created_at=now,
                                 updated_at=now)
        self.warehouses[warehouse.id] = warehouse
        return warehouse

    def update(self, organization_id, warehouse_id, data: WarehouseUpdate):
        existing = self.warehouses.get(warehouse_id)
        if existing is None:
            return None
        updated = existing.model_copy(update=data.model_dump(exclude_unset=True))
        self.warehouses[warehouse_id] = updated
        return updated

    def get_by_id(self, organization_id, warehouse_id):
        return self.warehouses.get(warehouse_id)

    def code_exists(self, organization_id, code, exclude_id=None):
        return any(w.code == code and w.id != exclude_id for w in self.warehouses.values())

    def list_all(self, organization_id):
        return list(self.warehouses.values())


class FakeProductRepository:
    """Only get_by_id is exercised by InventoryService — the rest aren't
    reachable through it, so they raise if accidentally called.
    """

    def __init__(self, products: dict[uuid.UUID, ProductOut] | None = None):
        self.products = products or {}

    def get_by_id(self, organization_id, product_id):
        return self.products.get(product_id)

    def create(self, *a, **k): raise NotImplementedError
    def update(self, *a, **k): raise NotImplementedError
    def sku_exists(self, *a, **k): raise NotImplementedError
    def barcode_exists(self, *a, **k): raise NotImplementedError
    def set_status(self, *a, **k): raise NotImplementedError
    def search(self, *a, **k): raise NotImplementedError


class FakeInventoryRepository:
    """Mimics the real repository's on-hand/reserved bookkeeping and
    negative-stock rule with a plain dict — no locking (single-threaded
    fake), just enough logic to prove the service calls it correctly and
    that InsufficientStockError/quantity rules are respected end to end.
    """

    def __init__(self, allow_negative: bool = False):
        self.allow_negative = allow_negative
        self.levels: dict[tuple, tuple[Decimal, Decimal]] = {}
        self.transactions: list[InventoryTransactionOut] = []

    def _key(self, product_id, warehouse_id):
        return (product_id, warehouse_id)

    def _apply(self, organization_id, product_id, warehouse_id, transaction_type,
              quantity_change, performed_by, notes=None):
        key = self._key(product_id, warehouse_id)
        on_hand, reserved = self.levels.get(key, (Decimal("0"), Decimal("0")))
        if transaction_type in (InventoryTransactionType.RESERVE,
                                InventoryTransactionType.RELEASE_RESERVE):
            new_reserved = reserved + quantity_change
            if new_reserved < 0 or new_reserved > on_hand:
                raise InsufficientStockError(product_id, warehouse_id, on_hand - reserved,
                                             quantity_change)
            reserved = new_reserved
        else:
            new_on_hand = on_hand + quantity_change
            if new_on_hand < 0 and not self.allow_negative:
                raise InsufficientStockError(product_id, warehouse_id, on_hand, -quantity_change)
            on_hand = new_on_hand
        self.levels[key] = (on_hand, reserved)
        tx = InventoryTransactionOut(
            id=uuid.uuid4(), product_id=product_id, warehouse_id=warehouse_id,
            transaction_type=transaction_type, quantity_change=quantity_change,
            quantity_on_hand_after=on_hand, quantity_reserved_after=reserved,
            reference_type=None, reference_id=None, performed_by=performed_by, notes=notes,
            created_at=datetime.now(timezone.utc))
        self.transactions.append(tx)
        return tx

    def stock_in(self, organization_id, product_id, warehouse_id, quantity, performed_by,
                notes=None):
        return self._apply(organization_id, product_id, warehouse_id,
                          InventoryTransactionType.STOCK_IN, quantity, performed_by, notes)

    def stock_out(self, organization_id, product_id, warehouse_id, quantity, performed_by,
                 notes=None):
        return self._apply(organization_id, product_id, warehouse_id,
                          InventoryTransactionType.STOCK_OUT, -quantity, performed_by, notes)

    def mark_damaged(self, organization_id, product_id, warehouse_id, quantity, performed_by,
                     notes=None):
        return self._apply(organization_id, product_id, warehouse_id,
                          InventoryTransactionType.DAMAGE, -quantity, performed_by, notes)

    def record_return(self, organization_id, product_id, warehouse_id, quantity, performed_by,
                      to_stock, notes=None):
        tx_type = (InventoryTransactionType.RETURN_IN if to_stock
                  else InventoryTransactionType.RETURN_OUT)
        signed = quantity if to_stock else -quantity
        return self._apply(organization_id, product_id, warehouse_id, tx_type, signed,
                          performed_by, notes)

    def adjust(self, organization_id, product_id, warehouse_id, quantity_change, reason,
              performed_by):
        return self._apply(organization_id, product_id, warehouse_id,
                          InventoryTransactionType.ADJUSTMENT, quantity_change, performed_by,
                          reason)

    def transfer(self, organization_id, product_id, from_warehouse_id, to_warehouse_id,
                quantity, performed_by, notes=None):
        out_tx = self._apply(organization_id, product_id, from_warehouse_id,
                            InventoryTransactionType.TRANSFER_OUT, -quantity, performed_by,
                            notes)
        in_tx = self._apply(organization_id, product_id, to_warehouse_id,
                           InventoryTransactionType.TRANSFER_IN, quantity, performed_by, notes)
        return out_tx, in_tx

    def reserve(self, organization_id, product_id, warehouse_id, quantity, performed_by,
               notes=None):
        return self._apply(organization_id, product_id, warehouse_id,
                          InventoryTransactionType.RESERVE, quantity, performed_by, notes)

    def release_reservation(self, organization_id, product_id, warehouse_id, quantity,
                            performed_by, notes=None):
        return self._apply(organization_id, product_id, warehouse_id,
                          InventoryTransactionType.RELEASE_RESERVE, -quantity, performed_by,
                          notes)

    def get_level(self, organization_id, product_id, warehouse_id) -> InventoryLevel:
        on_hand, reserved = self.levels.get(self._key(product_id, warehouse_id),
                                            (Decimal("0"), Decimal("0")))
        return InventoryLevel(product_id=product_id, warehouse_id=warehouse_id,
                              warehouse_code="WH", quantity_on_hand=on_hand,
                              quantity_reserved=reserved)

    def list_levels_for_product(self, organization_id, product_id):
        return [self.get_level(organization_id, product_id, wh_id)
               for (p_id, wh_id) in self.levels if p_id == product_id]

    def list_all_levels(self, organization_id):
        return [self.get_level(organization_id, p_id, wh_id)
               for (p_id, wh_id) in self.levels]

    def list_transactions(self, organization_id, filter: TransactionFilter):
        raise NotImplementedError  # not exercised by these tests


def _product(product_id=None) -> ProductOut:
    now = datetime.now(timezone.utc)
    return ProductOut(id=product_id or uuid.uuid4(), sku="SKU-1", barcode=None, name="Widget",
                      description=None, product_type="goods", category=None, brand=None,
                      unit=UNIT, sub_unit=None, sub_unit_conversion_factor=None,
                      tertiary_unit=None, tertiary_unit_conversion_factor=None,
                      purchase_price=Decimal("10"), selling_price=Decimal("15"),
                      tax_percent=Decimal("13"), is_taxable=True,
                      minimum_stock_level=Decimal("0"), hsn_code=None, size=None, color=None,
                      flavour=None, dftqc_no=None, country_of_origin=None, expiry_date=None,
                      status="active", created_at=now, updated_at=now)


def _service(permissions=ALL_PERMISSIONS, warehouses=None, inventory=None, products=None,
            allow_negative=False):
    warehouses = warehouses or FakeWarehouseRepository()
    inventory = inventory or FakeInventoryRepository(allow_negative=allow_negative)
    products = products or FakeProductRepository()
    sessions = SessionManager(idle_timeout=timedelta(minutes=30))
    sessions.start(user_id=uuid.uuid4(), organization_id=ORG_ID, role_id=uuid.uuid4(),
                   permissions=frozenset(permissions), is_superuser=False,
                   must_change_password=False, now=datetime.now(timezone.utc))
    return (InventoryService(warehouses, inventory, products, sessions),
           warehouses, inventory, products)


# -- warehouses --------------------------------------------------------- #

def test_create_warehouse_normalizes_code():
    service, _, _, _ = _service()
    created = service.create_warehouse(WarehouseCreate(code="  main-1  ", name="Main"))
    assert created.code == "MAIN-1"


def test_create_warehouse_rejects_duplicate_code():
    service, _, _, _ = _service()
    service.create_warehouse(WarehouseCreate(code="MAIN", name="Main"))
    with pytest.raises(DuplicateWarehouseCodeError):
        service.create_warehouse(WarehouseCreate(code="main", name="Main 2"))


def test_create_warehouse_requires_permission():
    service, _, _, _ = _service(permissions=frozenset())
    with pytest.raises(PermissionDeniedError):
        service.create_warehouse(WarehouseCreate(code="MAIN", name="Main"))


def test_get_warehouse_missing_raises_not_found():
    service, _, _, _ = _service()
    with pytest.raises(WarehouseNotFoundError):
        service.get_warehouse(uuid.uuid4())


# -- stock operations ----------------------------------------------------#

def _setup(allow_negative=False):
    service, warehouses, inventory, products = _service(allow_negative=allow_negative)
    warehouse = service.create_warehouse(WarehouseCreate(code="MAIN", name="Main"))
    product = _product()
    products.products[product.id] = product
    return service, warehouse, product


def test_stock_in_then_get_level_reflects_quantity():
    service, warehouse, product = _setup()
    service.stock_in(StockMoveRequest(product_id=product.id, warehouse_id=warehouse.id,
                                      quantity=Decimal("100")))
    level = service.get_inventory_level(product.id, warehouse.id)
    assert level.quantity_on_hand == Decimal("100")
    assert level.quantity_available == Decimal("100")


def test_stock_out_reduces_on_hand():
    service, warehouse, product = _setup()
    service.stock_in(StockMoveRequest(product_id=product.id, warehouse_id=warehouse.id,
                                      quantity=Decimal("100")))
    service.stock_out(StockMoveRequest(product_id=product.id, warehouse_id=warehouse.id,
                                       quantity=Decimal("20")))
    assert service.get_available_stock(product.id, warehouse.id) == Decimal("80")


def test_stock_out_beyond_on_hand_raises_insufficient_stock():
    service, warehouse, product = _setup()
    service.stock_in(StockMoveRequest(product_id=product.id, warehouse_id=warehouse.id,
                                      quantity=Decimal("10")))
    with pytest.raises(InsufficientStockError):
        service.stock_out(StockMoveRequest(product_id=product.id, warehouse_id=warehouse.id,
                                           quantity=Decimal("20")))


def test_stock_out_beyond_on_hand_allowed_when_negative_stock_enabled():
    service, warehouse, product = _setup(allow_negative=True)
    service.stock_out(StockMoveRequest(product_id=product.id, warehouse_id=warehouse.id,
                                       quantity=Decimal("5")))
    assert service.get_available_stock(product.id, warehouse.id) == Decimal("-5")


def test_stock_move_rejects_unknown_product():
    service, warehouse, _ = _setup()
    with pytest.raises(ProductNotFoundError):
        service.stock_in(StockMoveRequest(product_id=uuid.uuid4(), warehouse_id=warehouse.id,
                                          quantity=Decimal("1")))


def test_stock_move_rejects_unknown_warehouse():
    service, _, product = _setup()
    with pytest.raises(WarehouseNotFoundError):
        service.stock_in(StockMoveRequest(product_id=product.id, warehouse_id=uuid.uuid4(),
                                          quantity=Decimal("1")))


def test_stock_move_rejects_zero_quantity():
    service, warehouse, product = _setup()
    with pytest.raises(InventoryValidationError):
        service.stock_in(StockMoveRequest(product_id=product.id, warehouse_id=warehouse.id,
                                          quantity=Decimal("0")))


def test_mark_damaged_reduces_on_hand():
    service, warehouse, product = _setup()
    service.stock_in(StockMoveRequest(product_id=product.id, warehouse_id=warehouse.id,
                                      quantity=Decimal("10")))
    service.mark_damaged(StockMoveRequest(product_id=product.id, warehouse_id=warehouse.id,
                                          quantity=Decimal("3")))
    assert service.get_available_stock(product.id, warehouse.id) == Decimal("7")


def test_record_return_to_stock_increases_on_hand():
    service, warehouse, product = _setup()
    service.record_return(ReturnRequest(product_id=product.id, warehouse_id=warehouse.id,
                                        quantity=Decimal("4"), to_stock=True))
    assert service.get_available_stock(product.id, warehouse.id) == Decimal("4")


def test_record_return_to_supplier_decreases_on_hand():
    service, warehouse, product = _setup()
    service.stock_in(StockMoveRequest(product_id=product.id, warehouse_id=warehouse.id,
                                      quantity=Decimal("10")))
    service.record_return(ReturnRequest(product_id=product.id, warehouse_id=warehouse.id,
                                        quantity=Decimal("4"), to_stock=False))
    assert service.get_available_stock(product.id, warehouse.id) == Decimal("6")


def test_adjust_stock_applies_signed_delta():
    service, warehouse, product = _setup()
    service.stock_in(StockMoveRequest(product_id=product.id, warehouse_id=warehouse.id,
                                      quantity=Decimal("10")))
    service.adjust_stock(AdjustmentRequest(product_id=product.id, warehouse_id=warehouse.id,
                                           quantity_change=Decimal("-2"), reason="Recount"))
    assert service.get_available_stock(product.id, warehouse.id) == Decimal("8")


def test_adjust_stock_requires_reason():
    service, warehouse, product = _setup()
    with pytest.raises(InventoryValidationError):
        service.adjust_stock(AdjustmentRequest(product_id=product.id, warehouse_id=warehouse.id,
                                               quantity_change=Decimal("2"), reason="   "))


def test_transfer_moves_stock_between_warehouses():
    service, warehouse_a, product = _setup()
    warehouse_b = service.create_warehouse(WarehouseCreate(code="SECOND", name="Second"))
    service.stock_in(StockMoveRequest(product_id=product.id, warehouse_id=warehouse_a.id,
                                      quantity=Decimal("10")))

    service.transfer_stock(TransferRequest(product_id=product.id,
                                           from_warehouse_id=warehouse_a.id,
                                           to_warehouse_id=warehouse_b.id,
                                           quantity=Decimal("6")))

    assert service.get_available_stock(product.id, warehouse_a.id) == Decimal("4")
    assert service.get_available_stock(product.id, warehouse_b.id) == Decimal("6")


def test_transfer_rejects_same_source_and_destination():
    service, warehouse, product = _setup()
    with pytest.raises(InventoryValidationError):
        service.transfer_stock(TransferRequest(product_id=product.id,
                                               from_warehouse_id=warehouse.id,
                                               to_warehouse_id=warehouse.id,
                                               quantity=Decimal("1")))


def test_transfer_requires_inventory_transfer_permission():
    service, warehouse_a, product = _setup()
    warehouse_b = service.create_warehouse(WarehouseCreate(code="SECOND", name="Second"))
    limited, _, _, _ = _service(
        permissions=frozenset({"inventory.view", "inventory.adjust"}))
    with pytest.raises(PermissionDeniedError):
        limited.transfer_stock(TransferRequest(product_id=product.id,
                                               from_warehouse_id=warehouse_a.id,
                                               to_warehouse_id=warehouse_b.id,
                                               quantity=Decimal("1")))


def test_reserve_then_release_round_trips_available_stock():
    service, warehouse, product = _setup()
    service.stock_in(StockMoveRequest(product_id=product.id, warehouse_id=warehouse.id,
                                      quantity=Decimal("10")))
    service.reserve_stock(StockMoveRequest(product_id=product.id, warehouse_id=warehouse.id,
                                           quantity=Decimal("4")))
    assert service.get_available_stock(product.id, warehouse.id) == Decimal("6")

    service.release_reservation(StockMoveRequest(product_id=product.id,
                                                  warehouse_id=warehouse.id,
                                                  quantity=Decimal("4")))
    assert service.get_available_stock(product.id, warehouse.id) == Decimal("10")


def test_reserve_more_than_on_hand_raises_insufficient_stock():
    service, warehouse, product = _setup()
    service.stock_in(StockMoveRequest(product_id=product.id, warehouse_id=warehouse.id,
                                      quantity=Decimal("5")))
    with pytest.raises(InsufficientStockError):
        service.reserve_stock(StockMoveRequest(product_id=product.id, warehouse_id=warehouse.id,
                                               quantity=Decimal("6")))


def test_stock_operations_require_inventory_adjust_permission():
    service, warehouse, product = _setup()
    limited, _, _, _ = _service(permissions=frozenset({"inventory.view"}))
    with pytest.raises(PermissionDeniedError):
        limited.stock_in(StockMoveRequest(product_id=product.id, warehouse_id=warehouse.id,
                                          quantity=Decimal("1")))
