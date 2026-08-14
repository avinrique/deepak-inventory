import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel

from app.domain.pricing import line_subtotal
from app.domain.sales import (
    PaymentMethod,
    SalesOrderStatus,
    line_discount,
    line_subtotal_after_discount,
    line_tax_after_discount,
    line_total_after_discount,
)


class CustomerCreate(BaseModel):
    name: str
    contact_person: str | None = None
    phone: str | None = None
    email: str | None = None
    address: str | None = None
    tax_id: str | None = None
    notes: str | None = None


class CustomerUpdate(BaseModel):
    """All fields optional — a partial update only touches what's set."""
    name: str | None = None
    contact_person: str | None = None
    phone: str | None = None
    email: str | None = None
    address: str | None = None
    tax_id: str | None = None
    notes: str | None = None
    is_active: bool | None = None


class CustomerOut(BaseModel):
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


class SalesOrderItemInput(BaseModel):
    product_id: uuid.UUID
    quantity_ordered: Decimal
    unit_price: Decimal
    tax_percent: Decimal = Decimal("0")
    discount_percent: Decimal = Decimal("0")


class SalesOrderItemOut(BaseModel):
    id: uuid.UUID
    product_id: uuid.UUID
    quantity_ordered: Decimal
    quantity_fulfilled: Decimal
    unit_price: Decimal
    tax_percent: Decimal
    discount_percent: Decimal

    @property
    def quantity_outstanding(self) -> Decimal:
        return self.quantity_ordered - self.quantity_fulfilled

    @property
    def subtotal(self) -> Decimal:
        """List price before discount or tax."""
        return line_subtotal(self.quantity_ordered, self.unit_price)

    @property
    def discount_amount(self) -> Decimal:
        return line_discount(self.quantity_ordered, self.unit_price, self.discount_percent)

    @property
    def tax_amount(self) -> Decimal:
        """Computed on the post-discount price — see
        app.domain.sales.line_tax_after_discount.
        """
        return line_tax_after_discount(self.quantity_ordered, self.unit_price,
                                       self.discount_percent, self.tax_percent)

    @property
    def total(self) -> Decimal:
        return line_total_after_discount(self.quantity_ordered, self.unit_price,
                                         self.discount_percent, self.tax_percent)


class SalesOrderCreate(BaseModel):
    customer_id: uuid.UUID
    warehouse_id: uuid.UUID
    notes: str | None = None
    items: list[SalesOrderItemInput]


class SalesOrderUpdate(BaseModel):
    """Only valid while the order is DRAFT — items are replaced wholesale,
    not merged, so a partial update always supplies the full new item list.
    """
    customer_id: uuid.UUID | None = None
    warehouse_id: uuid.UUID | None = None
    notes: str | None = None
    items: list[SalesOrderItemInput] | None = None


class SalesOrderOut(BaseModel):
    id: uuid.UUID
    customer_id: uuid.UUID
    warehouse_id: uuid.UUID
    status: SalesOrderStatus
    notes: str | None
    created_by: uuid.UUID
    confirmed_by: uuid.UUID | None
    confirmed_at: datetime | None
    items: list[SalesOrderItemOut]
    created_at: datetime
    updated_at: datetime

    @property
    def subtotal(self) -> Decimal:
        return sum((item.subtotal for item in self.items), Decimal("0"))

    @property
    def discount_amount(self) -> Decimal:
        return sum((item.discount_amount for item in self.items), Decimal("0"))

    @property
    def tax_amount(self) -> Decimal:
        return sum((item.tax_amount for item in self.items), Decimal("0"))

    @property
    def total_amount(self) -> Decimal:
        return sum((item.total for item in self.items), Decimal("0"))


class SalesOrderFilter(BaseModel):
    customer_id: uuid.UUID | None = None
    status: SalesOrderStatus | None = None
    page: int = 1
    page_size: int = 25


class SalesOrderPage(BaseModel):
    items: list[SalesOrderOut]
    total: int
    page: int
    page_size: int

    @property
    def total_pages(self) -> int:
        if self.page_size <= 0:
            return 1
        return max(1, -(-self.total // self.page_size))  # ceil division


class InvoiceOut(BaseModel):
    id: uuid.UUID
    sales_order_id: uuid.UUID
    invoice_number: str
    subtotal: Decimal
    tax_amount: Decimal
    total_amount: Decimal
    generated_by: uuid.UUID
    generated_at: datetime


class PaymentRequest(BaseModel):
    invoice_id: uuid.UUID
    amount: Decimal
    method: PaymentMethod
    notes: str | None = None


class PaymentOut(BaseModel):
    id: uuid.UUID
    invoice_id: uuid.UUID
    amount: Decimal
    method: PaymentMethod
    recorded_by: uuid.UUID
    notes: str | None
    received_at: datetime


class SalesReturnRequest(BaseModel):
    sales_order_item_id: uuid.UUID
    quantity: Decimal
    reason: str


class SalesReturnOut(BaseModel):
    id: uuid.UUID
    sales_order_id: uuid.UUID
    sales_order_item_id: uuid.UUID
    warehouse_id: uuid.UUID
    product_id: uuid.UUID
    quantity: Decimal
    reason: str
    returned_by: uuid.UUID
    inventory_transaction_id: uuid.UUID
    returned_at: datetime
