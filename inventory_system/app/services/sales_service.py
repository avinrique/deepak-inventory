"""Application service for customers and the sales workflow (DRAFT ->
CONFIRMED -> FULFILLED -> COMPLETED, or -> CANCELLED). The one seam the UI
is allowed to call — status-machine legality, existence checks, and
permission enforcement live here; row locking and the atomic inventory-
ledger + audit-log write on fulfillment/return/payment live in
SalesOrderRepository (SQL-only, needs a real transaction).

organization_id and the acting user always come from the current session,
never from a caller-supplied argument. "Sales history" isn't a separate
method — it's search_sales_orders, the same method a live sales screen
would call, just without a narrowing filter. There is no complete_sale()
action: COMPLETED is only ever reached as a side effect of record_payment
once an invoice is fully paid (see app.domain.sales.ALLOWED_TRANSITIONS).
"""
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from app.core.exceptions import (
    CustomerNotFoundError,
    InvalidSalesOrderTransitionError,
    InvoiceNotFoundError,
    ProductNotFoundError,
    SalesOrderNotFoundError,
    SalesOrderValidationError,
    WarehouseNotFoundError,
)
from app.domain.sales import (
    SalesOrderStatus,
    can_transition,
    validate_customer,
    validate_sales_order_item,
)
from app.repositories.interfaces import (
    CustomerRepository,
    ProductRepository,
    SalesOrderRepository,
    WarehouseRepository,
)
from app.schemas.sales import (
    CustomerCreate,
    CustomerOut,
    CustomerUpdate,
    InvoiceDocumentData,
    InvoiceOut,
    PaymentOut,
    PaymentRequest,
    SalesOrderCreate,
    SalesOrderFilter,
    SalesOrderOut,
    SalesOrderPage,
    SalesOrderUpdate,
    SalesReturnOut,
)
from app.security.authorization import require_permission
from app.security.session import SessionManager


class SalesService:
    def __init__(self, customers: CustomerRepository, sales_orders: SalesOrderRepository,
                products: ProductRepository, warehouses: WarehouseRepository,
                sessions: SessionManager):
        self._customers = customers
        self._sales_orders = sales_orders
        self._products = products
        self._warehouses = warehouses
        self._sessions = sessions

    def _organization_id(self) -> uuid.UUID:
        return self._sessions.current(now=datetime.now(timezone.utc)).organization_id

    def _current_user_id(self) -> uuid.UUID:
        return self._sessions.current(now=datetime.now(timezone.utc)).user_id

    def _require_sales_order(self, sales_order_id: uuid.UUID) -> SalesOrderOut:
        so = self._sales_orders.get_by_id(self._organization_id(), sales_order_id)
        if so is None:
            raise SalesOrderNotFoundError(sales_order_id)
        return so

    # -- customers ------------------------------------------------------#
    @require_permission("sales.update")
    def create_customer(self, data: CustomerCreate) -> CustomerOut:
        errors = validate_customer(name=data.name)
        if errors:
            raise SalesOrderValidationError(errors)
        return self._customers.create(self._organization_id(), data)

    @require_permission("sales.update")
    def update_customer(self, customer_id: uuid.UUID, data: CustomerUpdate) -> CustomerOut:
        if data.name is not None:
            errors = validate_customer(name=data.name)
            if errors:
                raise SalesOrderValidationError(errors)
        result = self._customers.update(self._organization_id(), customer_id, data)
        if result is None:
            raise CustomerNotFoundError(customer_id)
        return result

    @require_permission("sales.read")
    def get_customer(self, customer_id: uuid.UUID) -> CustomerOut:
        result = self._customers.get_by_id(self._organization_id(), customer_id)
        if result is None:
            raise CustomerNotFoundError(customer_id)
        return result

    @require_permission("sales.read")
    def list_customers(self) -> list[CustomerOut]:
        return self._customers.list_all(self._organization_id())

    # -- sales orders -----------------------------------------------------#
    def _validate_items(self, data) -> None:
        org_id = self._organization_id()
        if not data.items:
            raise SalesOrderValidationError(["A sales order needs at least one line item."])
        errors: list[str] = []
        for item in data.items:
            if self._products.get_by_id(org_id, item.product_id) is None:
                raise ProductNotFoundError(item.product_id)
            errors.extend(validate_sales_order_item(
                quantity_ordered=item.quantity_ordered, unit_price=item.unit_price,
                tax_percent=item.tax_percent, discount_percent=item.discount_percent))
        if errors:
            raise SalesOrderValidationError(errors)

    @require_permission("sales.create")
    def create_sales_order(self, data: SalesOrderCreate) -> SalesOrderOut:
        org_id = self._organization_id()
        if self._customers.get_by_id(org_id, data.customer_id) is None:
            raise CustomerNotFoundError(data.customer_id)
        if self._warehouses.get_by_id(org_id, data.warehouse_id) is None:
            raise WarehouseNotFoundError(data.warehouse_id)
        self._validate_items(data)
        # Deliberately does not touch Inventory/InventoryTransaction at
        # all — a sales order reserves/deducts nothing until fulfillment.
        # See SalesOrderRepository.create and
        # tests/services/test_sales_service.py.
        return self._sales_orders.create(org_id, data, self._current_user_id())

    @require_permission("sales.update")
    def update_sales_order(self, sales_order_id: uuid.UUID,
                           data: SalesOrderUpdate) -> SalesOrderOut:
        existing = self._require_sales_order(sales_order_id)
        if existing.status != SalesOrderStatus.DRAFT:
            raise SalesOrderValidationError(["Only draft sales orders can be edited."])
        org_id = self._organization_id()
        if data.customer_id is not None and self._customers.get_by_id(
                org_id, data.customer_id) is None:
            raise CustomerNotFoundError(data.customer_id)
        if data.warehouse_id is not None and self._warehouses.get_by_id(
                org_id, data.warehouse_id) is None:
            raise WarehouseNotFoundError(data.warehouse_id)
        if data.items is not None:
            self._validate_items(data)
        result = self._sales_orders.update(org_id, sales_order_id, data)
        if result is None:
            raise SalesOrderNotFoundError(sales_order_id)
        return result

    @require_permission("sales.read")
    def get_sales_order(self, sales_order_id: uuid.UUID) -> SalesOrderOut:
        return self._require_sales_order(sales_order_id)

    @require_permission("sales.read")
    def search_sales_orders(self, filter: SalesOrderFilter) -> SalesOrderPage:
        return self._sales_orders.search(self._organization_id(), filter)

    def _transition_or_raise(self, sales_order_id: uuid.UUID,
                             target: SalesOrderStatus) -> SalesOrderOut:
        existing = self._require_sales_order(sales_order_id)
        if not can_transition(existing.status, target):
            raise InvalidSalesOrderTransitionError(existing.status, target)
        return existing

    @require_permission("sales.confirm")
    def confirm_sales_order(self, sales_order_id: uuid.UUID) -> SalesOrderOut:
        self._transition_or_raise(sales_order_id, SalesOrderStatus.CONFIRMED)
        result = self._sales_orders.confirm(self._organization_id(), sales_order_id,
                                            self._current_user_id())
        if result is None:
            raise SalesOrderNotFoundError(sales_order_id)
        return result

    @require_permission("sales.cancel")
    def cancel_sales_order(self, sales_order_id: uuid.UUID) -> SalesOrderOut:
        # No separate "already fulfilled" check needed: the state machine
        # (app.domain.sales.ALLOWED_TRANSITIONS) already excludes CANCELLED
        # as a target from FULFILLED/COMPLETED — same reasoning as
        # PurchaseService.cancel_purchase_order.
        self._transition_or_raise(sales_order_id, SalesOrderStatus.CANCELLED)
        result = self._sales_orders.cancel(self._organization_id(), sales_order_id)
        if result is None:
            raise SalesOrderNotFoundError(sales_order_id)
        return result

    @require_permission("sales.fulfill")
    def fulfill_sale(self, sales_order_id: uuid.UUID) -> SalesOrderOut:
        self._require_sales_order(sales_order_id)
        return self._sales_orders.fulfill_sale(self._organization_id(), sales_order_id,
                                               self._current_user_id())

    @require_permission("sales.invoice")
    def generate_invoice(self, sales_order_id: uuid.UUID) -> InvoiceOut:
        self._require_sales_order(sales_order_id)
        return self._sales_orders.generate_invoice(self._organization_id(), sales_order_id,
                                                   self._current_user_id())

    @require_permission("sales.read")
    def get_invoice_document(self, invoice_id: uuid.UUID) -> InvoiceDocumentData:
        # Viewing/(re)printing an already-generated invoice is a read
        # action, not a new financial one — sales.invoice gates *creating*
        # the invoice record (generate_invoice above); producing the PDF
        # again later (e.g. "Re-generate invoice" in the UI) doesn't touch
        # the financial record at all, it just re-renders the same data.
        result = self._sales_orders.get_invoice_document(self._organization_id(), invoice_id)
        if result is None:
            raise InvoiceNotFoundError(invoice_id)
        return result

    @require_permission("sales.payment")
    def record_payment(self, data: PaymentRequest) -> PaymentOut:
        if data.amount <= 0:
            raise SalesOrderValidationError(["Payment amount must be greater than zero."])
        return self._sales_orders.record_payment(self._organization_id(), data,
                                                  self._current_user_id())

    @require_permission("sales.refund")
    def record_sales_return(self, sales_order_id: uuid.UUID, sales_order_item_id: uuid.UUID,
                            quantity: Decimal, reason: str) -> SalesReturnOut:
        self._require_sales_order(sales_order_id)
        errors = []
        if quantity <= 0:
            errors.append("Quantity must be greater than zero.")
        if not reason.strip():
            errors.append("A reason is required for a sales return.")
        if errors:
            raise SalesOrderValidationError(errors)
        return self._sales_orders.record_return(
            self._organization_id(), sales_order_id, sales_order_item_id, quantity,
            reason.strip(), self._current_user_id())
