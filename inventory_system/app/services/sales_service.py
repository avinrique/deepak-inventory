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
import re
import secrets
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from app.core.exceptions import (
    CreditLimitExceededError,
    CustomerNotFoundError,
    DuplicateCustomerCodeError,
    DuplicateReferenceNumberError,
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
    line_total_after_discount,
    normalize_customer_code,
    validate_customer,
    validate_sales_order_item,
)
from app.repositories.interfaces import (
    AuditLogRepository,
    CustomerRepository,
    ProductRepository,
    SalesOrderRepository,
    WarehouseRepository,
)
from app.schemas.transactions import TransactionListPage, TransactionListRow
from app.schemas.sales import (
    CustomerBalance,
    CustomerCreate,
    CustomerOut,
    CustomerTransaction,
    CustomerUpdate,
    FinalizeSaleRequest,
    FinalizeSaleResult,
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
from app.security.authorization import check_permission, require_permission
from app.security.session import SessionManager

_MAX_CUSTOMER_CODE_SUFFIX_ATTEMPTS = 5


class SalesService:
    def __init__(self, customers: CustomerRepository, sales_orders: SalesOrderRepository,
                products: ProductRepository, warehouses: WarehouseRepository,
                sessions: SessionManager, audit_log: AuditLogRepository | None = None):
        self._customers = customers
        self._sales_orders = sales_orders
        self._products = products
        self._warehouses = warehouses
        self._sessions = sessions
        self._audit_log = audit_log

    def _organization_id(self) -> uuid.UUID:
        return self._sessions.current(now=datetime.now(timezone.utc)).organization_id

    def _current_user_id(self) -> uuid.UUID:
        return self._sessions.current(now=datetime.now(timezone.utc)).user_id

    def _require_sales_order(self, sales_order_id: uuid.UUID) -> SalesOrderOut:
        so = self._sales_orders.get_by_id(self._organization_id(), sales_order_id)
        if so is None:
            raise SalesOrderNotFoundError(sales_order_id)
        return so

    def _audit(self, *, action: str, entity_id: uuid.UUID,
              changes: dict | None = None) -> None:
        # Best-effort, separate from the write it describes — same
        # tradeoff app.services.user_service._audit documents (no single
        # row here to be atomic *with*, unlike fulfill_sale/receive_goods,
        # which post their audit entry inside the same DB transaction as
        # the inventory movement they're describing). audit_log is
        # optional only so existing callers/tests that construct
        # SalesService without one (nothing here needed it before customer
        # management) don't break — every real caller (Container) supplies
        # one.
        if self._audit_log is None:
            return
        session = self._sessions.peek()
        actor_id = session.user_id if session else None
        org_id = session.organization_id if session else None
        self._audit_log.record(organization_id=org_id, user_id=actor_id, actor_email=None,
                               organization_name=None, action=action, entity_type="customer",
                               entity_id=entity_id, changes=changes)

    # -- customers ------------------------------------------------------#
    def _derive_customer_code(self, name: str) -> str:
        """Mirrors UserService._derive_username: only used when the caller
        doesn't supply an explicit code — an explicit choice that collides
        is a DuplicateCustomerCodeError, not silently suffixed into
        something the caller didn't type.
        """
        org_id = self._organization_id()
        base = normalize_customer_code(re.sub(r"[^A-Za-z0-9]+", "-", name)).strip("-") or "CUST"
        candidate = base
        for _ in range(_MAX_CUSTOMER_CODE_SUFFIX_ATTEMPTS):
            if not self._customers.code_exists(org_id, candidate):
                return candidate
            candidate = f"{base}-{secrets.token_hex(2).upper()}"
        raise DuplicateCustomerCodeError(candidate)

    @require_permission("customers.create")
    def create_customer(self, data: CustomerCreate) -> CustomerOut:
        org_id = self._organization_id()
        explicit_code = (normalize_customer_code(data.customer_code)
                        if data.customer_code else None)
        errors = validate_customer(name=data.name, customer_code=explicit_code,
                                   email=data.email, phone=data.phone,
                                   credit_limit=data.credit_limit,
                                   opening_balance=data.opening_balance)
        if errors:
            raise SalesOrderValidationError(errors)

        if explicit_code is not None:
            if self._customers.code_exists(org_id, explicit_code):
                raise DuplicateCustomerCodeError(explicit_code)
            code = explicit_code
        else:
            code = self._derive_customer_code(data.name)

        created = self._customers.create(org_id, data.model_copy(update={"customer_code": code}),
                                         created_by=self._current_user_id())
        self._audit(action="customer.create", entity_id=created.id,
                   changes={"name": created.name, "customer_code": created.customer_code,
                           "customer_type": created.customer_type.value,
                           "credit_limit": str(created.credit_limit)
                           if created.credit_limit is not None else None})
        return created

    @require_permission("customers.update")
    def update_customer(self, customer_id: uuid.UUID, data: CustomerUpdate) -> CustomerOut:
        current = self._customers.get_by_id(self._organization_id(), customer_id)
        if current is None:
            raise CustomerNotFoundError(customer_id)

        normalized_code = (normalize_customer_code(data.customer_code)
                          if data.customer_code is not None else None)
        errors = validate_customer(
            name=data.name if data.name is not None else current.name,
            customer_code=normalized_code, email=data.email, phone=data.phone,
            credit_limit=data.credit_limit, opening_balance=data.opening_balance)
        if errors:
            raise SalesOrderValidationError(errors)

        if normalized_code is not None:
            if (normalized_code != current.customer_code
                    and self._customers.code_exists(self._organization_id(), normalized_code,
                                                    exclude_id=customer_id)):
                raise DuplicateCustomerCodeError(normalized_code)
            # Always persist the normalized form, even when it already
            # matches current.customer_code — otherwise re-saving the same
            # code in different casing (e.g. "mine" over "MINE") would
            # silently write the un-normalized text.
            data = data.model_copy(update={"customer_code": normalized_code})

        result = self._customers.update(self._organization_id(), customer_id, data)
        if result is None:
            raise CustomerNotFoundError(customer_id)

        self._audit_customer_diff(customer_id, current, result,
                                  data.model_dump(exclude_unset=True))
        return result

    def _audit_customer_diff(self, customer_id: uuid.UUID, current: CustomerOut,
                             result: CustomerOut, updated_fields: dict) -> None:
        before, after = {}, {}
        for field in updated_fields:
            old_value = getattr(current, field)
            new_value = getattr(result, field)
            if old_value != new_value:
                before[field] = str(old_value)
                after[field] = str(new_value)
        if before:
            self._audit(action="customer.update", entity_id=customer_id,
                       changes={"before": before, "after": after})
        # Credit limit gets its own named event on top of the generic
        # diff above — "important customer information changed" is
        # explicitly called out as its own thing to record, not just
        # folded silently into a generic update entry.
        if "credit_limit" in before:
            self._audit(action="customer.credit_limit_changed", entity_id=customer_id,
                       changes={"before": before["credit_limit"], "after": after["credit_limit"]})

    @require_permission("customers.deactivate")
    def deactivate_customer(self, customer_id: uuid.UUID) -> CustomerOut:
        result = self._customers.update(self._organization_id(), customer_id,
                                        CustomerUpdate(is_active=False))
        if result is None:
            raise CustomerNotFoundError(customer_id)
        self._audit(action="customer.deactivate", entity_id=customer_id)
        return result

    @require_permission("customers.deactivate")
    def activate_customer(self, customer_id: uuid.UUID) -> CustomerOut:
        result = self._customers.update(self._organization_id(), customer_id,
                                        CustomerUpdate(is_active=True))
        if result is None:
            raise CustomerNotFoundError(customer_id)
        self._audit(action="customer.activate", entity_id=customer_id)
        return result

    @require_permission("customers.view")
    def get_customer(self, customer_id: uuid.UUID) -> CustomerOut:
        result = self._customers.get_by_id(self._organization_id(), customer_id)
        if result is None:
            raise CustomerNotFoundError(customer_id)
        return result

    @require_permission("customers.view")
    def list_customers(self) -> list[CustomerOut]:
        return self._customers.list_all(self._organization_id())

    @require_permission("customers.view")
    def get_customer_balance(self, customer_id: uuid.UUID) -> CustomerBalance:
        result = self._customers.get_balance(self._organization_id(), customer_id)
        if result is None:
            raise CustomerNotFoundError(customer_id)
        return result

    @require_permission("customers.view")
    def get_customer_history(self, customer_id: uuid.UUID) -> list[CustomerTransaction]:
        if self._customers.get_by_id(self._organization_id(), customer_id) is None:
            raise CustomerNotFoundError(customer_id)
        return self._customers.get_history(self._organization_id(), customer_id)

    @require_permission("customers.export")
    def export_customers(self) -> list[CustomerOut]:
        # Same permission-gated read as list_customers, just named for
        # what the UI does with it (CSV export via app.reporting.export,
        # not a separate query) — kept as its own method so
        # customers.export can be denied independently of customers.view.
        return self._customers.list_all(self._organization_id())

    def _check_credit_limit(self, customer: CustomerOut, new_order_total: Decimal) -> None:
        if customer.credit_limit is None:
            return  # no limit configured -> no credit control enforced
        balance = self._customers.get_balance(self._organization_id(), customer.id)
        available = balance.credit_limit - balance.outstanding_balance
        if new_order_total > available:
            raise CreditLimitExceededError(available, new_order_total)

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
                tax_percent=item.tax_percent, discount_percent=item.discount_percent,
                excise_percent=item.excise_percent))
        if errors:
            raise SalesOrderValidationError(errors)

    @require_permission("sales.create")
    def create_sales_order(self, data: SalesOrderCreate) -> SalesOrderOut:
        org_id = self._organization_id()
        customer = self._customers.get_by_id(org_id, data.customer_id)
        if customer is None:
            raise CustomerNotFoundError(data.customer_id)
        if self._warehouses.get_by_id(org_id, data.warehouse_id) is None:
            raise WarehouseNotFoundError(data.warehouse_id)
        self._validate_items(data)
        if data.reference_number and self._sales_orders.reference_number_exists(
                org_id, data.reference_number):
            raise DuplicateReferenceNumberError(data.reference_number)

        # Credit control: enforced here, not just hidden in the UI — a
        # caller going straight to the service (a script, a bug, a future
        # second front-end) hits the same block. No-op for a customer with
        # credit_limit=None (the "no limit configured" convention — see
        # app.models.customer's docstring).
        order_total = sum(
            (line_total_after_discount(item.quantity_ordered, item.unit_price,
                                       item.discount_percent, item.tax_percent,
                                       excise_percent=item.excise_percent)
            for item in data.items), Decimal("0"))
        self._check_credit_limit(customer, order_total)

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
        if data.reference_number is not None and self._sales_orders.reference_number_exists(
                org_id, data.reference_number, exclude_id=sales_order_id):
            raise DuplicateReferenceNumberError(data.reference_number)
        result = self._sales_orders.update(org_id, sales_order_id, data)
        if result is None:
            raise SalesOrderNotFoundError(sales_order_id)
        return result

    @require_permission("sales.view")
    def get_sales_order(self, sales_order_id: uuid.UUID) -> SalesOrderOut:
        return self._require_sales_order(sales_order_id)

    @require_permission("sales.view")
    def search_sales_orders(self, filter: SalesOrderFilter) -> SalesOrderPage:
        return self._sales_orders.search(self._organization_id(), filter)

    @require_permission("sales.view")
    def list_sales_transactions(self, filter: SalesOrderFilter) -> TransactionListPage:
        """The Sales register — same filter shape as search_sales_orders,
        but flattened rows plus totals over the whole filtered set. See
        app.repositories.sql.transaction_list.
        """
        return self._sales_orders.list_transactions(self._organization_id(), filter)

    @require_permission("sales.view")
    def export_sales_transactions(self, filter: SalesOrderFilter) -> list[TransactionListRow]:
        return self._sales_orders.export_transactions(self._organization_id(), filter)

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
        result = self._sales_orders.cancel(self._organization_id(), sales_order_id,
                                           self._current_user_id())
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

    @require_permission("sales.view")
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

    @require_permission("sales.view")
    def get_invoice_by_sales_order(self, sales_order_id: uuid.UUID) -> InvoiceOut | None:
        """None (not an error) when no invoice has been generated yet for
        this order — callers (e.g. the Sales page's "View Invoice" action)
        use this to distinguish "not generated yet" from a real failure.
        """
        return self._sales_orders.get_invoice_by_sales_order(self._organization_id(),
                                                              sales_order_id)

    @require_permission("sales.payment")
    def record_payment(self, data: PaymentRequest) -> PaymentOut:
        if data.amount <= 0:
            raise SalesOrderValidationError(["Payment amount must be greater than zero."])
        return self._sales_orders.record_payment(self._organization_id(), data,
                                                  self._current_user_id())

    def finalize_new_bill(self, sales_order_id: uuid.UUID,
                         data: FinalizeSaleRequest) -> FinalizeSaleResult:
        """The "New Bill: Save as Sale" action — atomically confirms,
        fulfills, and invoices a DRAFT sales order, optionally recording a
        payment too, all as one database transaction (see
        SalesOrderRepository.finalize_new_bill: if any step fails, nothing
        is applied — no stock deducted, no invoice/payment created).

        Requires every individual permission each step would need on its
        own (sales.confirm, sales.fulfill, sales.invoice — and
        sales.payment too, only when a payment is actually included) —
        there's no separate umbrella permission for this, deliberately
        matching how app.security.permissions keeps sales.* granular. A
        role missing any one of those can't finalize a bill in this single
        step, exactly as they couldn't complete the equivalent sequence of
        individual actions on the Sales Orders page either — this is a
        UI-workflow shortcut, not a new grant of authority.
        """
        session = self._sessions.current(now=datetime.now(timezone.utc))
        for code in ("sales.confirm", "sales.fulfill", "sales.invoice"):
            check_permission(session, code)

        existing = self._require_sales_order(sales_order_id)
        if existing.status != SalesOrderStatus.DRAFT:
            raise SalesOrderValidationError(
                ["Only a draft sales order can be finalized into a sale."])

        errors = []
        if data.overall_discount_amount < 0:
            errors.append("Overall discount cannot be negative.")
        if data.other_charges < 0:
            errors.append("Other charges cannot be negative.")
        if data.payment_amount is not None:
            check_permission(session, "sales.payment")
            if data.payment_amount <= 0:
                errors.append("Payment amount must be greater than zero.")
            if data.payment_method is None:
                errors.append("Select a payment method to record a payment.")
        if errors:
            raise SalesOrderValidationError(errors)

        return self._sales_orders.finalize_new_bill(
            self._organization_id(), sales_order_id, self._current_user_id(), data)

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
