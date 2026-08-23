import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel

from app.domain.purchasing import PurchaseOrderStatus, line_subtotal, line_tax, line_total


class SupplierCreate(BaseModel):
    name: str
    contact_person: str | None = None
    phone: str | None = None
    email: str | None = None
    address: str | None = None
    tax_id: str | None = None
    notes: str | None = None


class SupplierUpdate(BaseModel):
    """All fields optional — a partial update only touches what's set."""
    name: str | None = None
    contact_person: str | None = None
    phone: str | None = None
    email: str | None = None
    address: str | None = None
    tax_id: str | None = None
    notes: str | None = None
    is_active: bool | None = None


class SupplierOut(BaseModel):
    id: uuid.UUID
    name: str
    contact_person: str | None
    phone: str | None
    email: str | None
    address: str | None
    tax_id: str | None
    notes: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime


class PurchaseOrderItemInput(BaseModel):
    product_id: uuid.UUID
    quantity_ordered: Decimal
    unit_price: Decimal
    tax_percent: Decimal = Decimal("0")


class PurchaseOrderItemOut(BaseModel):
    id: uuid.UUID
    product_id: uuid.UUID
    quantity_ordered: Decimal
    quantity_received: Decimal
    unit_price: Decimal
    tax_percent: Decimal

    @property
    def quantity_outstanding(self) -> Decimal:
        return self.quantity_ordered - self.quantity_received

    @property
    def subtotal(self) -> Decimal:
        return line_subtotal(self.quantity_ordered, self.unit_price)

    @property
    def tax_amount(self) -> Decimal:
        return line_tax(self.quantity_ordered, self.unit_price, self.tax_percent)

    @property
    def total(self) -> Decimal:
        return line_total(self.quantity_ordered, self.unit_price, self.tax_percent)


class PurchaseOrderCreate(BaseModel):
    supplier_id: uuid.UUID
    warehouse_id: uuid.UUID
    supplier_invoice_number: str | None = None
    reference_number: str | None = None
    expected_date: date | None = None
    notes: str | None = None
    custom_fields: dict[str, str] | None = None
    items: list[PurchaseOrderItemInput]


class PurchaseOrderUpdate(BaseModel):
    """Only valid while the order is DRAFT — items are replaced wholesale,
    not merged, so a partial update always supplies the full new item list.
    """
    supplier_id: uuid.UUID | None = None
    warehouse_id: uuid.UUID | None = None
    supplier_invoice_number: str | None = None
    reference_number: str | None = None
    expected_date: date | None = None
    notes: str | None = None
    custom_fields: dict[str, str] | None = None
    items: list[PurchaseOrderItemInput] | None = None


class PurchaseOrderOut(BaseModel):
    id: uuid.UUID
    order_number: str | None
    supplier_invoice_number: str | None = None
    reference_number: str | None = None
    supplier_id: uuid.UUID
    warehouse_id: uuid.UUID
    status: PurchaseOrderStatus
    expected_date: date | None
    notes: str | None
    custom_fields: dict[str, str] | None
    created_by: uuid.UUID
    approved_by: uuid.UUID | None
    approved_at: datetime | None
    items: list[PurchaseOrderItemOut]
    created_at: datetime
    updated_at: datetime

    @property
    def subtotal(self) -> Decimal:
        return sum((item.subtotal for item in self.items), Decimal("0"))

    @property
    def tax_amount(self) -> Decimal:
        return sum((item.tax_amount for item in self.items), Decimal("0"))

    @property
    def total_amount(self) -> Decimal:
        return sum((item.total for item in self.items), Decimal("0"))


class PurchaseOrderFilter(BaseModel):
    supplier_id: uuid.UUID | None = None
    status: PurchaseOrderStatus | None = None
    # Case-insensitive substring match across order number, supplier
    # invoice number, reference number, and supplier name.
    search: str | None = None
    # Both bounds inclusive, against created_at. The dates are interpreted
    # as whole UTC days — see app.repositories.sql.transaction_list.
    date_from: date | None = None
    date_to: date | None = None
    # Resolved against a whitelist in transaction_list; an unrecognised
    # value falls back to created_at rather than reaching SQL.
    sort_by: str = "created_at"
    sort_desc: bool = True
    page: int = 1
    page_size: int = 25


class PurchaseOrderPage(BaseModel):
    items: list[PurchaseOrderOut]
    total: int
    page: int
    page_size: int

    @property
    def total_pages(self) -> int:
        if self.page_size <= 0:
            return 1
        return max(1, -(-self.total // self.page_size))  # ceil division


class GoodsReceiptLineInput(BaseModel):
    purchase_order_item_id: uuid.UUID
    quantity: Decimal


class ReceiveGoodsRequest(BaseModel):
    purchase_order_id: uuid.UUID
    lines: list[GoodsReceiptLineInput]
    notes: str | None = None


class GoodsReceiptItemOut(BaseModel):
    id: uuid.UUID
    purchase_order_item_id: uuid.UUID
    quantity_received: Decimal
    inventory_transaction_id: uuid.UUID


class GoodsReceiptOut(BaseModel):
    id: uuid.UUID
    purchase_order_id: uuid.UUID
    warehouse_id: uuid.UUID
    received_by: uuid.UUID
    notes: str | None
    received_at: datetime
    items: list[GoodsReceiptItemOut]


class PurchaseReturnRequest(BaseModel):
    purchase_order_item_id: uuid.UUID
    quantity: Decimal
    reason: str


class PurchaseReturnOut(BaseModel):
    id: uuid.UUID
    purchase_order_id: uuid.UUID
    purchase_order_item_id: uuid.UUID
    warehouse_id: uuid.UUID
    product_id: uuid.UUID
    quantity: Decimal
    reason: str
    returned_by: uuid.UUID
    inventory_transaction_id: uuid.UUID
    returned_at: datetime
