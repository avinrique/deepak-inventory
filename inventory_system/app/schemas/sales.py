import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel

from app.domain.pricing import line_subtotal
from app.domain.sales import (
    CustomerType,
    InvoicePaymentStatus,
    PaymentMethod,
    SalesOrderStatus,
    line_discount,
    line_subtotal_after_discount,
    line_tax_after_discount,
    line_total_after_discount,
)


class CustomerCreate(BaseModel):
    name: str
    customer_code: str | None = None  # None -> auto-derived from name, see SalesService
    customer_type: CustomerType = CustomerType.INDIVIDUAL
    contact_person: str | None = None
    phone: str | None = None
    email: str | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None
    country: str | None = None
    tax_id: str | None = None
    credit_limit: Decimal | None = None
    opening_balance: Decimal = Decimal("0")
    notes: str | None = None


class CustomerUpdate(BaseModel):
    """All fields optional — a partial update only touches what's set."""
    name: str | None = None
    customer_code: str | None = None
    customer_type: CustomerType | None = None
    contact_person: str | None = None
    phone: str | None = None
    email: str | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None
    country: str | None = None
    tax_id: str | None = None
    credit_limit: Decimal | None = None
    opening_balance: Decimal | None = None
    notes: str | None = None
    is_active: bool | None = None


class CustomerOut(BaseModel):
    id: uuid.UUID
    customer_code: str | None
    name: str
    customer_type: CustomerType
    contact_person: str | None
    phone: str | None
    email: str | None
    address: str | None
    city: str | None
    state: str | None
    country: str | None
    tax_id: str | None
    credit_limit: Decimal | None
    opening_balance: Decimal
    notes: str | None
    is_active: bool
    created_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class CustomerBalance(BaseModel):
    """Read-only, derived — never stored.

    outstanding_balance = opening_balance + invoiced_total +
    pending_orders_total - paid_total - returns_credit_total. Two different
    kinds of exposure are tracked separately on purpose: invoiced_total is
    what's on generated Invoices (frozen financial documents);
    pending_orders_total is every non-CANCELLED SalesOrder that hasn't been
    invoiced yet (DRAFT/CONFIRMED, or FULFILLED-but-not-yet-invoiced) — a
    customer's credit exposure the moment they commit to an order, not only
    once Billing gets around to invoicing it. Without pending_orders_total,
    credit control could be walked around entirely by stacking multiple
    uninvoiced draft orders. returns_credit_total is the sum of
    SalesReturn.credit_amount — goods a customer returned reduce what they
    owe, the same as a payment would, even though no Payment row exists for
    it (a return is a credit, not cash received). Every figure here is
    summed from what SalesOrder/Invoice/Payment/SalesReturn already
    recorded (via the same app.domain.sales line-total formula every
    order/invoice line already uses) — see SqlCustomerRepository.get_balance,
    which never computes a price or tax amount from scratch, only totals
    what Billing recorded.
    """
    customer_id: uuid.UUID
    opening_balance: Decimal
    invoiced_total: Decimal
    pending_orders_total: Decimal
    paid_total: Decimal
    returns_credit_total: Decimal = Decimal("0")
    outstanding_balance: Decimal
    credit_limit: Decimal | None

    @property
    def available_credit(self) -> Decimal | None:
        if self.credit_limit is None:
            return None
        return self.credit_limit - self.outstanding_balance


class CustomerTransaction(BaseModel):
    """One row in a customer's transaction history — sales orders,
    invoices, payments, and returns all normalized into one shape so the
    UI has a single timeline to render instead of four separate lists.
    """
    kind: str  # "sales_order" | "invoice" | "payment" | "return"
    id: uuid.UUID
    reference: str  # order number / invoice number / payment method / return reason
    amount: Decimal | None
    status: str | None
    occurred_at: datetime


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
    discount_amount: Decimal
    overall_discount_amount: Decimal
    tax_amount: Decimal
    other_charges: Decimal
    total_amount: Decimal
    due_date: date | None
    generated_by: uuid.UUID
    generated_at: datetime


class InvoiceDocumentLine(BaseModel):
    """One line of a printable/exportable invoice document — a flattened,
    display-ready view of a SalesOrderItem (product name/sku resolved,
    every money figure precomputed) so the PDF template
    (app.reports.sales_invoice_pdf) only has to lay values out, never
    compute them.
    """
    sku: str
    product_name: str
    quantity: Decimal
    unit_price: Decimal
    discount_percent: Decimal
    tax_percent: Decimal
    line_subtotal: Decimal    # quantity * unit_price, before discount
    line_discount: Decimal
    line_tax: Decimal         # computed on the post-discount price
    line_total: Decimal


class InvoiceDocumentData(BaseModel):
    """Everything app.reports.sales_invoice_pdf.render_invoice_pdf needs to
    lay out one invoice — assembled once by
    SalesOrderRepository.get_invoice_document (a single read joining
    Invoice + SalesOrder + Customer + Organization + items + payments) so
    the template stays a pure function of this data, with no repository or
    service access of its own. Company fields come from the organization's
    Settings (app.services.organization_service) — never hardcoded.
    """
    # company (from Settings — see app.services.organization_service)
    company_name: str
    company_legal_name: str | None
    company_address: str | None
    company_phone: str | None
    company_email: str | None
    company_website: str | None
    company_tax_id: str | None
    # invoice
    invoice_id: uuid.UUID
    invoice_number: str
    invoice_date: datetime
    due_date: date | None
    sales_order_id: uuid.UUID
    # customer
    customer_name: str
    customer_address: str | None
    customer_phone: str | None
    customer_email: str | None
    customer_tax_id: str | None
    # lines + totals
    items: list[InvoiceDocumentLine]
    subtotal: Decimal
    discount_total: Decimal
    overall_discount: Decimal
    tax_total: Decimal
    other_charges: Decimal
    total: Decimal
    amount_paid: Decimal
    amount_due: Decimal
    payment_status: InvoicePaymentStatus
    notes: str | None


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


class FinalizeSaleRequest(BaseModel):
    """The "New Bill: Save as Sale" request — confirm + fulfill + generate
    the invoice as one atomic step (see SalesOrderRepository.
    finalize_new_bill). Recording a payment in the same step is optional:
    leave payment_amount unset to finalize an unpaid/on-credit bill and
    record payment later via the existing record_payment (e.g. from the
    Sales Orders page's Record Payment action, or the New Bill screen's
    own Record Payment button once the invoice exists).
    """
    due_date: date | None = None
    overall_discount_amount: Decimal = Decimal("0")
    other_charges: Decimal = Decimal("0")
    payment_amount: Decimal | None = None
    payment_method: PaymentMethod | None = None
    payment_notes: str | None = None


class FinalizeSaleResult(BaseModel):
    sales_order: SalesOrderOut
    invoice: InvoiceOut
    payment: PaymentOut | None


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
    # The monetary value credited back to the customer for this return —
    # quantity priced at the original order line's unit_price/discount/tax,
    # via the same app.domain.sales.line_total_after_discount formula the
    # line was originally invoiced with. Netted against the customer's
    # outstanding_balance in SqlCustomerRepository.get_balance so a return
    # actually reduces what they owe, not just their stock count.
    credit_amount: Decimal
    returned_at: datetime
