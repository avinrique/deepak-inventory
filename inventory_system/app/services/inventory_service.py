"""Application service for warehouses and the inventory ledger — the one
seam the UI (and, eventually, Sales/Purchases services) is allowed to
call. Quantity math, row locking, and the immutable-transaction guarantee
live in InventoryRepository (SQL-only — real row locking needs a real
database, there is no Excel equivalent). This layer's job is validation,
existence checks with friendly errors instead of raw FK failures, and
permission enforcement — organization_id and performed_by always come from
the current session, never from a caller-supplied argument.
"""
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from app.core.exceptions import (
    DuplicateWarehouseCodeError,
    InventoryValidationError,
    ProductNotFoundError,
    WarehouseNotFoundError,
)
from app.domain.inventory import normalize_warehouse_code, validate_quantity, validate_warehouse
from app.repositories.interfaces import InventoryRepository, ProductRepository, WarehouseRepository
from app.schemas.inventory import (
    AdjustmentRequest,
    InventoryLevel,
    InventoryTransactionOut,
    InventoryTransactionPage,
    ReturnRequest,
    StockMoveRequest,
    TransactionFilter,
    TransferRequest,
    WarehouseCreate,
    WarehouseOut,
    WarehouseUpdate,
)
from app.security.authorization import require_permission
from app.security.session import SessionManager


class InventoryService:
    def __init__(self, warehouses: WarehouseRepository, inventory: InventoryRepository,
                products: ProductRepository, sessions: SessionManager):
        self._warehouses = warehouses
        self._inventory = inventory
        self._products = products
        self._sessions = sessions

    def _organization_id(self) -> uuid.UUID:
        return self._sessions.current(now=datetime.now(timezone.utc)).organization_id

    def _current_user_id(self) -> uuid.UUID:
        return self._sessions.current(now=datetime.now(timezone.utc)).user_id

    def _require_warehouse(self, warehouse_id: uuid.UUID) -> WarehouseOut:
        warehouse = self._warehouses.get_by_id(self._organization_id(), warehouse_id)
        if warehouse is None:
            raise WarehouseNotFoundError(warehouse_id)
        return warehouse

    def _require_product(self, product_id: uuid.UUID) -> None:
        if self._products.get_by_id(self._organization_id(), product_id) is None:
            raise ProductNotFoundError(product_id)

    # -- warehouses --------------------------------------------------- #
    @require_permission("warehouse.manage")
    def create_warehouse(self, data: WarehouseCreate) -> WarehouseOut:
        org_id = self._organization_id()
        code = normalize_warehouse_code(data.code)
        errors = validate_warehouse(code=code, name=data.name)
        if errors:
            raise InventoryValidationError(errors)
        if self._warehouses.code_exists(org_id, code):
            raise DuplicateWarehouseCodeError(code)
        return self._warehouses.create(org_id, data.model_copy(update={"code": code}))

    @require_permission("warehouse.manage")
    def update_warehouse(self, warehouse_id: uuid.UUID, data: WarehouseUpdate) -> WarehouseOut:
        org_id = self._organization_id()
        existing = self._require_warehouse(warehouse_id)
        updates = data.model_dump(exclude_unset=True)
        code = normalize_warehouse_code(updates["code"]) if updates.get("code") else existing.code
        name = updates.get("name", existing.name)

        errors = validate_warehouse(code=code, name=name)
        if errors:
            raise InventoryValidationError(errors)
        if code != existing.code and self._warehouses.code_exists(org_id, code,
                                                                   exclude_id=warehouse_id):
            raise DuplicateWarehouseCodeError(code)

        if "code" in updates:
            updates["code"] = code
        result = self._warehouses.update(org_id, warehouse_id, WarehouseUpdate(**updates))
        if result is None:
            raise WarehouseNotFoundError(warehouse_id)
        return result

    @require_permission("inventory.view")
    def get_warehouse(self, warehouse_id: uuid.UUID) -> WarehouseOut:
        return self._require_warehouse(warehouse_id)

    @require_permission("inventory.view")
    def list_warehouses(self) -> list[WarehouseOut]:
        return self._warehouses.list_all(self._organization_id())

    # -- stock-changing operations ------------------------------------ #
    @require_permission("inventory.adjust")
    def stock_in(self, data: StockMoveRequest) -> InventoryTransactionOut:
        self._validate_move(data)
        return self._inventory.stock_in(self._organization_id(), data.product_id,
                                        data.warehouse_id, data.quantity,
                                        self._current_user_id(), notes=data.notes)

    @require_permission("inventory.adjust")
    def stock_out(self, data: StockMoveRequest) -> InventoryTransactionOut:
        self._validate_move(data)
        return self._inventory.stock_out(self._organization_id(), data.product_id,
                                         data.warehouse_id, data.quantity,
                                         self._current_user_id(), notes=data.notes)

    @require_permission("inventory.adjust")
    def mark_damaged(self, data: StockMoveRequest) -> InventoryTransactionOut:
        self._validate_move(data)
        return self._inventory.mark_damaged(self._organization_id(), data.product_id,
                                            data.warehouse_id, data.quantity,
                                            self._current_user_id(), notes=data.notes)

    @require_permission("inventory.adjust")
    def record_return(self, data: ReturnRequest) -> InventoryTransactionOut:
        self._validate_move(data)
        return self._inventory.record_return(self._organization_id(), data.product_id,
                                             data.warehouse_id, data.quantity,
                                             self._current_user_id(), data.to_stock,
                                             notes=data.notes)

    @require_permission("inventory.adjust")
    def adjust_stock(self, data: AdjustmentRequest) -> InventoryTransactionOut:
        self._require_product(data.product_id)
        self._require_warehouse(data.warehouse_id)
        errors = validate_quantity(data.quantity_change, allow_negative=True)
        if not data.reason.strip():
            errors.append("A reason is required for a manual adjustment.")
        if errors:
            raise InventoryValidationError(errors)
        return self._inventory.adjust(self._organization_id(), data.product_id,
                                      data.warehouse_id, data.quantity_change,
                                      data.reason.strip(), self._current_user_id())

    @require_permission("inventory.transfer")
    def transfer_stock(self, data: TransferRequest) -> tuple[InventoryTransactionOut,
                                                              InventoryTransactionOut]:
        self._require_product(data.product_id)
        self._require_warehouse(data.from_warehouse_id)
        self._require_warehouse(data.to_warehouse_id)
        errors = validate_quantity(data.quantity)
        if data.from_warehouse_id == data.to_warehouse_id:
            errors.append("Source and destination warehouse must be different.")
        if errors:
            raise InventoryValidationError(errors)
        return self._inventory.transfer(self._organization_id(), data.product_id,
                                        data.from_warehouse_id, data.to_warehouse_id,
                                        data.quantity, self._current_user_id(),
                                        notes=data.notes)

    @require_permission("inventory.adjust")
    def reserve_stock(self, data: StockMoveRequest) -> InventoryTransactionOut:
        self._validate_move(data)
        return self._inventory.reserve(self._organization_id(), data.product_id,
                                       data.warehouse_id, data.quantity,
                                       self._current_user_id(), notes=data.notes)

    @require_permission("inventory.adjust")
    def release_reservation(self, data: StockMoveRequest) -> InventoryTransactionOut:
        self._validate_move(data)
        return self._inventory.release_reservation(self._organization_id(), data.product_id,
                                                    data.warehouse_id, data.quantity,
                                                    self._current_user_id(), notes=data.notes)

    def _validate_move(self, data: StockMoveRequest) -> None:
        self._require_product(data.product_id)
        self._require_warehouse(data.warehouse_id)
        errors = validate_quantity(data.quantity)
        if errors:
            raise InventoryValidationError(errors)

    # -- reads ---------------------------------------------------------#
    @require_permission("inventory.view")
    def get_inventory_level(self, product_id: uuid.UUID,
                            warehouse_id: uuid.UUID) -> InventoryLevel:
        self._require_product(product_id)
        self._require_warehouse(warehouse_id)
        return self._inventory.get_level(self._organization_id(), product_id, warehouse_id)

    @require_permission("inventory.view")
    def get_levels_for_products(self, product_ids: list[uuid.UUID],
                                warehouse_id: uuid.UUID) -> dict[uuid.UUID, InventoryLevel]:
        """Bulk counterpart to get_inventory_level — one query for a whole
        result set (e.g. product-suggestion rows) instead of one per id.
        Deliberately skips _require_product's per-id existence check: the
        ids here come from a trusted prior search_products() result, not a
        caller-supplied arbitrary id, so re-validating each one would just
        reintroduce the N+1 this method exists to avoid. An id that somehow
        doesn't exist is simply absent from get_levels_for_products' own
        query and gets zero-filled like any other product with no stock
        row yet — never an error.
        """
        self._require_warehouse(warehouse_id)
        return self._inventory.get_levels_for_products(self._organization_id(), product_ids,
                                                        warehouse_id)

    @require_permission("inventory.view")
    def list_levels_for_product(self, product_id: uuid.UUID) -> list[InventoryLevel]:
        self._require_product(product_id)
        return self._inventory.list_levels_for_product(self._organization_id(), product_id)

    @require_permission("inventory.view")
    def list_all_levels(self) -> list[InventoryLevel]:
        return self._inventory.list_all_levels(self._organization_id())

    @require_permission("inventory.view")
    def get_available_stock(self, product_id: uuid.UUID, warehouse_id: uuid.UUID) -> Decimal:
        level = self.get_inventory_level(product_id, warehouse_id)
        return level.quantity_available

    @require_permission("inventory.view")
    def list_transactions(self, filter: TransactionFilter) -> InventoryTransactionPage:
        return self._inventory.list_transactions(self._organization_id(), filter)
