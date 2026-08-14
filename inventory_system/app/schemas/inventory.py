import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel

from app.domain.inventory import InventoryTransactionType


class WarehouseCreate(BaseModel):
    code: str
    name: str
    address: str | None = None


class WarehouseUpdate(BaseModel):
    """All fields optional — a partial update only touches what's set."""
    code: str | None = None
    name: str | None = None
    address: str | None = None
    is_active: bool | None = None


class WarehouseOut(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    address: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime


class InventoryLevel(BaseModel):
    """Current snapshot for one (product, warehouse) pair. ``available`` is
    derived (on_hand - reserved), never stored.
    """
    product_id: uuid.UUID
    warehouse_id: uuid.UUID
    warehouse_code: str
    quantity_on_hand: Decimal
    quantity_reserved: Decimal

    @property
    def quantity_available(self) -> Decimal:
        return self.quantity_on_hand - self.quantity_reserved


class InventoryTransactionOut(BaseModel):
    id: uuid.UUID
    product_id: uuid.UUID
    warehouse_id: uuid.UUID
    transaction_type: InventoryTransactionType
    quantity_change: Decimal
    quantity_on_hand_after: Decimal
    quantity_reserved_after: Decimal
    reference_type: str | None
    reference_id: uuid.UUID | None
    performed_by: uuid.UUID
    notes: str | None
    created_at: datetime


class StockMoveRequest(BaseModel):
    """Shared shape for stock in / stock out / damage / reserve / release —
    a single product+warehouse+unsigned quantity, with an optional note.
    Direction and sign come from which InventoryService method is called,
    not from this schema.
    """
    product_id: uuid.UUID
    warehouse_id: uuid.UUID
    quantity: Decimal
    notes: str | None = None


class ReturnRequest(StockMoveRequest):
    # True: a customer returning goods (+stock). False: returning goods to
    # a supplier (-stock).
    to_stock: bool = True


class AdjustmentRequest(BaseModel):
    product_id: uuid.UUID
    warehouse_id: uuid.UUID
    quantity_change: Decimal   # signed — positive or negative correction
    reason: str


class TransferRequest(BaseModel):
    product_id: uuid.UUID
    from_warehouse_id: uuid.UUID
    to_warehouse_id: uuid.UUID
    quantity: Decimal
    notes: str | None = None


class TransactionFilter(BaseModel):
    product_id: uuid.UUID | None = None
    warehouse_id: uuid.UUID | None = None
    transaction_type: InventoryTransactionType | None = None
    page: int = 1
    page_size: int = 50


class InventoryTransactionPage(BaseModel):
    items: list[InventoryTransactionOut]
    total: int
    page: int
    page_size: int
