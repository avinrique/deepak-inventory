"""SQLAlchemy-backed SalesOrderRepository.

fulfill_sale and record_return are the methods that must be atomic across
inventory + sales-order state + audit trail — same shape as
PurchaseOrderRepository.receive_goods/record_return, and for the same
reason: both reuse app.repositories.sql.inventory_repository's module-
level ``_apply`` helper directly inside their own ``with get_session()``
block instead of calling InventoryRepository (which would open its own,
separate transaction). If a validation fails partway — insufficient stock
on one line of a multi-line order, an over-return — the whole operation
rolls back.

fulfill_sale differs from receive_goods in one way: there is no partial-
fulfillment feature here (the workflow is a straight line, not a branch
like Purchasing's PARTIALLY_RECEIVED), so it deducts every order line's
full quantity_ordered in one shot rather than taking a caller-supplied list
of lines.

generate_invoice's numbering is the other transactional concern unique to
this module: InvoiceSequence is a per-organization counter, locked
(SELECT ... FOR UPDATE) and incremented in the same transaction as the
Invoice row it numbers, so two concurrent invoice generations for the same
organization can't collide, and a failed generation doesn't burn a number
(a native Postgres SEQUENCE would, since its counter doesn't roll back
with the transaction — see app.models.sales_order.InvoiceSequence).

confirm/fulfill_sale/generate_invoice/record_payment are each also
available pre-extracted as private, session-scoped helpers (``_confirm``,
``_fulfill``, ``_generate_invoice``, ``_record_payment_on_invoice``) that
take an already-open ``db`` rather than opening their own — the public
methods below are thin wrappers around them. finalize_new_bill (the "New
Bill: Save as Sale" action) is what actually needs this: it calls all four
helpers back-to-back inside ONE ``with get_session()`` block, so
confirm+fulfill+invoice(+payment) either land together or roll back
together — a partial finalize (e.g. stock deducted but invoice generation
failing) is not a state this method can produce. The public methods' own
behavior/signatures are unchanged by this — each still opens exactly one
transaction per call, exactly as before.
"""
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.core.exceptions import (
    CreditLimitExceededError,
    DuplicateInvoiceError,
    DuplicateReferenceNumberError,
    InvalidSalesOrderTransitionError,
    InvoiceNotFoundError,
    OverpaymentError,
    SalesOrderItemNotFoundError,
    SalesOrderNotFoundError,
    SalesOrderValidationError,
)
from app.database.session import get_session
from app.domain.inventory import InventoryTransactionType
from app.domain.pricing import line_subtotal
from app.domain.sales import (
    SalesOrderStatus,
    can_transition,
    compute_payment_status,
    format_invoice_number,
    line_discount,
    line_excise_after_discount,
    line_tax_after_discount,
    line_total_after_discount,
)
from app.models import (
    Customer,
    Invoice,
    InvoiceSequence,
    Organization,
    Payment,
    Product,
    SalesOrder,
    SalesOrderItem,
    SalesReturn,
    User,
)
from app.repositories.sql import transaction_list
from app.repositories.sql.audit_log_repository import record_audit_log
from app.repositories.sql.constraint_utils import constraint_name
from app.repositories.sql.customer_repository import compute_customer_balance
from app.repositories.sql.inventory_repository import _apply
from app.schemas.transactions import TransactionListPage, TransactionListRow
from app.schemas.sales import (
    FinalizeSaleRequest,
    FinalizeSaleResult,
    InvoiceDocumentData,
    InvoiceDocumentLine,
    InvoiceOut,
    PaymentOut,
    PaymentRequest,
    SalesOrderCreate,
    SalesOrderFilter,
    SalesOrderItemOut,
    SalesOrderOut,
    SalesOrderPage,
    SalesOrderUpdate,
    SalesReturnOut,
)


def _item_to_out(item: SalesOrderItem) -> SalesOrderItemOut:
    return SalesOrderItemOut(id=item.id, product_id=item.product_id,
                             quantity_ordered=item.quantity_ordered,
                             quantity_fulfilled=item.quantity_fulfilled,
                             unit_price=item.unit_price, tax_percent=item.tax_percent,
                             discount_percent=item.discount_percent,
                             excise_percent=item.excise_percent)


def _to_out(so: SalesOrder) -> SalesOrderOut:
    return SalesOrderOut(
        id=so.id, customer_id=so.customer_id, warehouse_id=so.warehouse_id, status=so.status,
        notes=so.notes, delivery_date=so.delivery_date, reference_number=so.reference_number,
        custom_fields=so.custom_fields, created_by=so.created_by, confirmed_by=so.confirmed_by,
        confirmed_at=so.confirmed_at, items=[_item_to_out(i) for i in so.items],
        created_at=so.created_at, updated_at=so.updated_at)


def _invoice_to_out(invoice: Invoice) -> InvoiceOut:
    return InvoiceOut(id=invoice.id, sales_order_id=invoice.sales_order_id,
                      invoice_number=invoice.invoice_number, subtotal=invoice.subtotal,
                      discount_amount=invoice.discount_amount,
                      overall_discount_amount=invoice.overall_discount_amount,
                      tax_amount=invoice.tax_amount, excise_amount=invoice.excise_amount,
                      other_charges=invoice.other_charges,
                      total_amount=invoice.total_amount, due_date=invoice.due_date,
                      generated_by=invoice.generated_by, generated_at=invoice.generated_at)


def _payment_to_out(payment: Payment) -> PaymentOut:
    return PaymentOut(id=payment.id, invoice_id=payment.invoice_id, amount=payment.amount,
                      method=payment.method, recorded_by=payment.recorded_by,
                      notes=payment.notes, received_at=payment.received_at)


def _return_to_out(r: SalesReturn) -> SalesReturnOut:
    return SalesReturnOut(id=r.id, sales_order_id=r.sales_order_id,
                          sales_order_item_id=r.sales_order_item_id,
                          warehouse_id=r.warehouse_id, product_id=r.product_id,
                          quantity=r.quantity, reason=r.reason, returned_by=r.returned_by,
                          inventory_transaction_id=r.inventory_transaction_id,
                          credit_amount=r.credit_amount, returned_at=r.returned_at)


def _lock_or_create_invoice_sequence(db, organization_id: uuid.UUID) -> InvoiceSequence:
    seq = (db.query(InvoiceSequence).filter_by(organization_id=organization_id)
          .with_for_update().first())
    if seq is not None:
        return seq

    # First invoice ever generated for this organization — same
    # SAVEPOINT-protected create as
    # inventory_repository._lock_or_create_inventory_row, for the same
    # concurrent-first-write race.
    savepoint = db.begin_nested()
    try:
        seq = InvoiceSequence(organization_id=organization_id, next_value=1)
        db.add(seq)
        db.flush()
        savepoint.commit()
        return seq
    except IntegrityError:
        savepoint.rollback()
        seq = (db.query(InvoiceSequence).filter_by(organization_id=organization_id)
              .with_for_update().first())
        if seq is None:
            raise
        return seq


def _audit_actor(db, organization_id: uuid.UUID, actor_id: uuid.UUID) -> tuple:
    user = db.get(User, actor_id)
    org = db.get(Organization, organization_id)
    return (user.email if user else None), (org.name if org else None)


def _confirm(db, so: SalesOrder, confirmed_by: uuid.UUID) -> None:
    so.status = SalesOrderStatus.CONFIRMED
    so.confirmed_by = confirmed_by
    so.confirmed_at = datetime.now(timezone.utc)
    db.flush()


def _fulfill(db, organization_id: uuid.UUID, so: SalesOrder, fulfilled_by: uuid.UUID, *,
            write_audit: bool = True) -> list[SalesOrderItem]:
    if so.status != SalesOrderStatus.CONFIRMED:
        raise InvalidSalesOrderTransitionError(so.status, SalesOrderStatus.FULFILLED)

    items = (db.query(SalesOrderItem)
            .filter_by(sales_order_id=so.id)
            .with_for_update()
            .all())
    for item in items:
        _apply(db, organization_id=organization_id, product_id=item.product_id,
              warehouse_id=so.warehouse_id, transaction_type=InventoryTransactionType.SALE,
              quantity_change=-item.quantity_ordered, performed_by=fulfilled_by,
              reference_type="sales_order_item", reference_id=item.id,
              enforce_low_stock_policy=True)
        item.quantity_fulfilled = item.quantity_ordered

    so.status = SalesOrderStatus.FULFILLED
    db.flush()

    if write_audit:
        actor_email, org_name = _audit_actor(db, organization_id, fulfilled_by)
        record_audit_log(
            db, organization_id=organization_id, user_id=fulfilled_by,
            actor_email=actor_email, organization_name=org_name,
            action="sales_order.fulfill", entity_type="sales_order", entity_id=so.id,
            changes={"items": [{"product_id": str(i.product_id),
                                "quantity": str(i.quantity_ordered)} for i in items]})
        db.flush()
    return items


def _generate_invoice(db, organization_id: uuid.UUID, so: SalesOrder, generated_by: uuid.UUID, *,
                      due_date=None, overall_discount_amount: Decimal = Decimal("0"),
                      other_charges: Decimal = Decimal("0"),
                      write_audit: bool = True) -> Invoice:
    if so.status != SalesOrderStatus.FULFILLED:
        raise SalesOrderValidationError(
            ["An invoice can only be generated for a fulfilled sales order."])
    if db.query(Invoice).filter_by(sales_order_id=so.id).first() is not None:
        raise DuplicateInvoiceError(so.id)

    subtotal = sum((line_subtotal(i.quantity_ordered, i.unit_price) for i in so.items),
                  Decimal("0"))
    discount_amount = sum((line_discount(i.quantity_ordered, i.unit_price,
                                         i.discount_percent) for i in so.items),
                          Decimal("0"))
    tax_amount = sum((line_tax_after_discount(i.quantity_ordered, i.unit_price,
                                               i.discount_percent, i.tax_percent)
                     for i in so.items), Decimal("0"))
    excise_amount = sum((line_excise_after_discount(i.quantity_ordered, i.unit_price,
                                                     i.discount_percent, i.excise_percent)
                        for i in so.items), Decimal("0"))
    line_totals = sum((line_total_after_discount(i.quantity_ordered, i.unit_price,
                                                  i.discount_percent, i.tax_percent,
                                                  excise_percent=i.excise_percent)
                      for i in so.items), Decimal("0"))
    total_amount = line_totals - overall_discount_amount + other_charges

    org = db.get(Organization, organization_id)
    seq = _lock_or_create_invoice_sequence(db, organization_id)
    number = format_invoice_number(org.invoice_number_prefix, seq.next_value)
    seq.next_value += 1

    invoice = Invoice(organization_id=organization_id, sales_order_id=so.id,
                     invoice_number=number, subtotal=subtotal,
                     discount_amount=discount_amount,
                     overall_discount_amount=overall_discount_amount, tax_amount=tax_amount,
                     excise_amount=excise_amount,
                     other_charges=other_charges, total_amount=total_amount,
                     due_date=due_date, generated_by=generated_by)
    db.add(invoice)
    db.flush()

    if write_audit:
        actor_email, org_name = _audit_actor(db, organization_id, generated_by)
        record_audit_log(
            db, organization_id=organization_id, user_id=generated_by,
            actor_email=actor_email, organization_name=org_name,
            action="sales_order.generate_invoice", entity_type="invoice",
            entity_id=invoice.id,
            changes={"sales_order_id": str(so.id), "invoice_number": number,
                    "total_amount": str(total_amount)})
        db.flush()
    return invoice


def _record_payment_on_invoice(db, organization_id: uuid.UUID, invoice: Invoice,
                               data: PaymentRequest, recorded_by: uuid.UUID, *,
                               write_audit: bool = True) -> Payment:
    """Assumes ``invoice`` is already the right row to charge against —
    either freshly locked by the caller via a query with
    ``.with_for_update()`` (the standalone record_payment below) or
    freshly inserted earlier in the SAME transaction (finalize_new_bill —
    nothing outside this transaction can see it yet, so no separate lock
    is needed there).
    """
    already_paid = sum(
        (p.amount for p in db.query(Payment).filter_by(invoice_id=invoice.id).all()),
        Decimal("0"))
    outstanding = invoice.total_amount - already_paid
    if data.amount > outstanding:
        raise OverpaymentError(data.amount, outstanding)

    payment = Payment(organization_id=organization_id, invoice_id=invoice.id,
                     amount=data.amount, method=data.method,
                     recorded_by=recorded_by, notes=data.notes)
    db.add(payment)
    db.flush()

    paid_sum = already_paid + data.amount
    if paid_sum >= invoice.total_amount:
        so = (db.query(SalesOrder).filter_by(id=invoice.sales_order_id)
             .with_for_update().first())
        if so is not None and can_transition(so.status, SalesOrderStatus.COMPLETED):
            so.status = SalesOrderStatus.COMPLETED

    if write_audit:
        actor_email, org_name = _audit_actor(db, organization_id, recorded_by)
        record_audit_log(
            db, organization_id=organization_id, user_id=recorded_by,
            actor_email=actor_email, organization_name=org_name,
            action="sales_order.record_payment", entity_type="invoice",
            entity_id=invoice.id,
            changes={"amount": str(data.amount), "method": data.method.value,
                    "total_paid": str(paid_sum)})
        db.flush()
    return payment


class SqlSalesOrderRepository:
    def create(self, organization_id: uuid.UUID, data: SalesOrderCreate,
              created_by: uuid.UUID) -> SalesOrderOut:
        """SalesService.create_sales_order already runs an unlocked credit
        check before calling this (fast-fail for the common case, and what
        the fake-repository service-layer tests exercise). That check alone
        has a TOCTOU race: two concurrent calls for the same customer can
        both read the same balance, both pass, and jointly exceed the
        limit. The lock below is the authoritative, race-free check for the
        real database — mirrors how record_payment locks the Invoice row
        rather than trusting an earlier unlocked balance read.
        """
        with get_session() as db:
            customer = (db.query(Customer)
                       .filter_by(id=data.customer_id, organization_id=organization_id)
                       .with_for_update().first())
            if customer is not None and customer.credit_limit is not None:
                order_total = sum(
                    (line_total_after_discount(item.quantity_ordered, item.unit_price,
                                               item.discount_percent, item.tax_percent,
                                               excise_percent=item.excise_percent)
                    for item in data.items), Decimal("0"))
                balance = compute_customer_balance(db, organization_id, customer)
                available = balance.credit_limit - balance.outstanding_balance
                if order_total > available:
                    raise CreditLimitExceededError(available, order_total)

            so = SalesOrder(organization_id=organization_id, customer_id=data.customer_id,
                            warehouse_id=data.warehouse_id, status=SalesOrderStatus.DRAFT,
                            notes=data.notes, delivery_date=data.delivery_date,
                            reference_number=data.reference_number,
                            custom_fields=data.custom_fields, created_by=created_by)
            db.add(so)
            try:
                db.flush()
            except IntegrityError as exc:
                if constraint_name(exc) == "ix_sales_orders_org_reference_number":
                    raise DuplicateReferenceNumberError(data.reference_number) from exc
                raise
            for item in data.items:
                db.add(SalesOrderItem(sales_order_id=so.id, product_id=item.product_id,
                                     quantity_ordered=item.quantity_ordered,
                                     unit_price=item.unit_price,
                                     tax_percent=item.tax_percent,
                                     discount_percent=item.discount_percent,
                                     excise_percent=item.excise_percent))
            db.flush()
            return _to_out(so)

    def update(self, organization_id: uuid.UUID, sales_order_id: uuid.UUID,
              data: SalesOrderUpdate) -> SalesOrderOut | None:
        with get_session() as db:
            so = db.get(SalesOrder, sales_order_id, with_for_update=True)
            if so is None or so.organization_id != organization_id:
                return None
            # Re-checked here, locked, as defense in depth: SalesService
            # already checks status == DRAFT before calling update(), but
            # that check happens in a separate, unlocked read — without
            # this re-check under the row lock, a concurrent confirm()
            # could land between the service's check and this transaction,
            # letting items be deleted/replaced on an order that is no
            # longer DRAFT (possibly already fulfilled against the old
            # items). Same convention as finalize_new_bill's own re-check.
            if so.status != SalesOrderStatus.DRAFT:
                raise SalesOrderValidationError(["Only draft sales orders can be edited."])
            for field, value in data.model_dump(exclude_unset=True, exclude={"items"}).items():
                setattr(so, field, value)
            if data.items is not None:
                for existing in list(so.items):
                    db.delete(existing)
                db.flush()
                for item in data.items:
                    db.add(SalesOrderItem(sales_order_id=so.id, product_id=item.product_id,
                                         quantity_ordered=item.quantity_ordered,
                                         unit_price=item.unit_price,
                                         tax_percent=item.tax_percent,
                                         discount_percent=item.discount_percent,
                                         excise_percent=item.excise_percent))
            # Captured before the flush attempt: a failed flush leaves the
            # session's transaction rolled back, and re-reading an
            # attribute off `so` at that point would trigger a lazy-load
            # against a dead transaction (PendingRollbackError) instead of
            # returning the pending in-memory value.
            attempted_reference_number = so.reference_number

            # Explicit flush even when only non-item fields changed — without
            # it, a reference_number collision would only surface as a raw
            # IntegrityError at get_session()'s implicit commit, bypassing
            # the DuplicateReferenceNumberError translation below.
            try:
                db.flush()
            except IntegrityError as exc:
                if constraint_name(exc) == "ix_sales_orders_org_reference_number":
                    raise DuplicateReferenceNumberError(attempted_reference_number) from exc
                raise
            if data.items is not None:
                # See PurchaseOrderRepository.update — the already-loaded
                # so.items collection is stale relative to what was just
                # deleted/inserted directly, so it must be expired before
                # _to_out(so) reads it.
                db.expire(so, ["items"])
            return _to_out(so)

    def get_by_id(self, organization_id: uuid.UUID,
                 sales_order_id: uuid.UUID) -> SalesOrderOut | None:
        with get_session() as db:
            so = db.get(SalesOrder, sales_order_id)
            if so is None or so.organization_id != organization_id:
                return None
            return _to_out(so)

    def list_transactions(self, organization_id: uuid.UUID,
                          filter: SalesOrderFilter) -> TransactionListPage:
        """The sales register — flattened rows plus totals over the whole
        filtered set. See app.repositories.sql.transaction_list for why the
        money is summed in SQL rather than Python.
        """
        with get_session() as db:
            return transaction_list.sales_list(db, organization_id, filter)

    def export_transactions(self, organization_id: uuid.UUID,
                            filter: SalesOrderFilter) -> list[TransactionListRow]:
        with get_session() as db:
            return transaction_list.sales_export_rows(db, organization_id, filter)

    def search(self, organization_id: uuid.UUID,
              filter: SalesOrderFilter) -> SalesOrderPage:
        with get_session() as db:
            query = db.query(SalesOrder).filter(SalesOrder.organization_id == organization_id)
            if filter.customer_id:
                query = query.filter(SalesOrder.customer_id == filter.customer_id)
            if filter.status:
                query = query.filter(SalesOrder.status == filter.status)

            total = query.count()
            # _to_out touches so.items, which lazy-loads one query per row
            # without this — 25 extra round-trips on a default page.
            query = query.options(selectinload(SalesOrder.items))
            query = query.order_by(SalesOrder.created_at.desc())

            page = max(1, filter.page)
            page_size = max(1, filter.page_size)
            rows = query.offset((page - 1) * page_size).limit(page_size).all()

            return SalesOrderPage(items=[_to_out(s) for s in rows], total=total, page=page,
                                  page_size=page_size)

    def reference_number_exists(self, organization_id: uuid.UUID, reference_number: str,
                                exclude_id: uuid.UUID | None = None) -> bool:
        with get_session() as db:
            query = db.query(SalesOrder).filter_by(organization_id=organization_id,
                                                    reference_number=reference_number)
            if exclude_id is not None:
                query = query.filter(SalesOrder.id != exclude_id)
            return db.query(query.exists()).scalar()

    def _transition(self, organization_id: uuid.UUID, sales_order_id: uuid.UUID,
                    target: SalesOrderStatus) -> SalesOrderOut | None:
        with get_session() as db:
            so = db.get(SalesOrder, sales_order_id)
            if so is None or so.organization_id != organization_id:
                return None
            so.status = target
            db.flush()
            return _to_out(so)

    def confirm(self, organization_id: uuid.UUID, sales_order_id: uuid.UUID,
              confirmed_by: uuid.UUID) -> SalesOrderOut | None:
        with get_session() as db:
            so = db.get(SalesOrder, sales_order_id, with_for_update=True)
            if so is None or so.organization_id != organization_id:
                return None
            # Locked re-check, same reasoning as update()/cancel(): the
            # service layer already validates the transition against an
            # unlocked read, so a concurrent transition on this same order
            # (e.g. a second confirm, or a cancel) must be re-validated
            # once this transaction actually holds the row.
            if not can_transition(so.status, SalesOrderStatus.CONFIRMED):
                raise InvalidSalesOrderTransitionError(so.status, SalesOrderStatus.CONFIRMED)
            _confirm(db, so, confirmed_by)
            return _to_out(so)

    def cancel(self, organization_id: uuid.UUID, sales_order_id: uuid.UUID,
              cancelled_by: uuid.UUID) -> SalesOrderOut | None:
        with get_session() as db:
            so = db.get(SalesOrder, sales_order_id, with_for_update=True)
            if so is None or so.organization_id != organization_id:
                return None
            if not can_transition(so.status, SalesOrderStatus.CANCELLED):
                raise InvalidSalesOrderTransitionError(so.status, SalesOrderStatus.CANCELLED)
            previous_status = so.status
            so.status = SalesOrderStatus.CANCELLED
            db.flush()

            actor_email, org_name = _audit_actor(db, organization_id, cancelled_by)
            record_audit_log(
                db, organization_id=organization_id, user_id=cancelled_by,
                actor_email=actor_email, organization_name=org_name,
                action="sales_order.cancel", entity_type="sales_order", entity_id=so.id,
                changes={"before": {"status": previous_status.value},
                        "after": {"status": SalesOrderStatus.CANCELLED.value}})

            db.flush()
            return _to_out(so)

    def fulfill_sale(self, organization_id: uuid.UUID, sales_order_id: uuid.UUID,
                    fulfilled_by: uuid.UUID) -> SalesOrderOut:
        with get_session() as db:
            # Locked before _fulfill reads so.status: without this,
            # concurrent fulfill_sale calls on the same order could both
            # observe status == CONFIRMED before either commits (the
            # SalesOrderItem row lock inside _fulfill serializes the
            # inventory deduction itself, but the second caller's
            # already-read so.status is never refreshed after waiting on
            # that lock, so it would double-deduct without this).
            so = db.get(SalesOrder, sales_order_id, with_for_update=True)
            if so is None or so.organization_id != organization_id:
                raise SalesOrderNotFoundError(sales_order_id)
            _fulfill(db, organization_id, so, fulfilled_by)
            return _to_out(so)

    def generate_invoice(self, organization_id: uuid.UUID, sales_order_id: uuid.UUID,
                        generated_by: uuid.UUID) -> InvoiceOut:
        with get_session() as db:
            so = db.get(SalesOrder, sales_order_id)
            if so is None or so.organization_id != organization_id:
                raise SalesOrderNotFoundError(sales_order_id)
            invoice = _generate_invoice(db, organization_id, so, generated_by)
            return _invoice_to_out(invoice)

    def get_invoice(self, organization_id: uuid.UUID,
                   invoice_id: uuid.UUID) -> InvoiceOut | None:
        with get_session() as db:
            invoice = db.get(Invoice, invoice_id)
            if invoice is None or invoice.organization_id != organization_id:
                return None
            return _invoice_to_out(invoice)

    def get_invoice_by_sales_order(self, organization_id: uuid.UUID,
                                   sales_order_id: uuid.UUID) -> InvoiceOut | None:
        with get_session() as db:
            invoice = (db.query(Invoice)
                      .filter_by(organization_id=organization_id,
                                sales_order_id=sales_order_id)
                      .first())
            return _invoice_to_out(invoice) if invoice is not None else None

    def get_invoice_document(self, organization_id: uuid.UUID,
                            invoice_id: uuid.UUID) -> InvoiceDocumentData | None:
        with get_session() as db:
            invoice = db.get(Invoice, invoice_id)
            if invoice is None or invoice.organization_id != organization_id:
                return None
            so = db.get(SalesOrder, invoice.sales_order_id)
            customer = db.get(Customer, so.customer_id)
            org = db.get(Organization, organization_id)

            item_rows = (db.query(SalesOrderItem, Product)
                        .join(Product, SalesOrderItem.product_id == Product.id)
                        .filter(SalesOrderItem.sales_order_id == so.id)
                        .order_by(SalesOrderItem.created_at)
                        .all())
            lines = []
            for item, product in item_rows:
                subtotal = line_subtotal(item.quantity_ordered, item.unit_price)
                discount = line_discount(item.quantity_ordered, item.unit_price,
                                         item.discount_percent)
                tax = line_tax_after_discount(item.quantity_ordered, item.unit_price,
                                              item.discount_percent, item.tax_percent)
                excise = line_excise_after_discount(item.quantity_ordered, item.unit_price,
                                                    item.discount_percent, item.excise_percent)
                total = line_total_after_discount(item.quantity_ordered, item.unit_price,
                                                  item.discount_percent, item.tax_percent,
                                                  excise_percent=item.excise_percent)
                lines.append(InvoiceDocumentLine(
                    sku=product.sku, product_name=product.name, quantity=item.quantity_ordered,
                    unit_price=item.unit_price, discount_percent=item.discount_percent,
                    tax_percent=item.tax_percent, excise_percent=item.excise_percent,
                    line_subtotal=subtotal, line_discount=discount,
                    line_tax=tax, line_excise=excise, line_total=total))

            paid = (db.query(func.coalesce(func.sum(Payment.amount), 0))
                   .filter(Payment.invoice_id == invoice.id).scalar())
            paid = Decimal(paid) if paid is not None else Decimal("0")
            amount_due = invoice.total_amount - paid
            status = compute_payment_status(invoice.total_amount, paid)

            return InvoiceDocumentData(
                company_name=org.name, company_legal_name=org.legal_name,
                company_address=org.address, company_phone=org.phone,
                company_email=org.email, company_website=org.website,
                company_tax_id=org.tax_id,
                invoice_id=invoice.id, invoice_number=invoice.invoice_number,
                invoice_date=invoice.generated_at, due_date=invoice.due_date,
                sales_order_id=so.id,
                customer_name=customer.name, customer_address=customer.address,
                customer_phone=customer.phone, customer_email=customer.email,
                customer_tax_id=customer.tax_id,
                items=lines, subtotal=invoice.subtotal, discount_total=invoice.discount_amount,
                overall_discount=invoice.overall_discount_amount, tax_total=invoice.tax_amount,
                excise_total=invoice.excise_amount,
                other_charges=invoice.other_charges, total=invoice.total_amount,
                amount_paid=paid, amount_due=amount_due, payment_status=status, notes=so.notes)

    def record_payment(self, organization_id: uuid.UUID, data: PaymentRequest,
                      recorded_by: uuid.UUID) -> PaymentOut:
        with get_session() as db:
            # Locks the Invoice row *before* reading existing payments or
            # inserting the new one, so two concurrent record_payment calls
            # against the same invoice are fully serialized — the second
            # caller blocks until the first commits, and then sees the
            # first payment's effect on the outstanding balance. Locking
            # only the Payment rows (the previous approach) doesn't close
            # this: with zero existing payments there's nothing yet to
            # lock, so two concurrent *first* payments could each
            # independently pass an outstanding-balance check that, taken
            # together, overpays the invoice.
            invoice = (db.query(Invoice).filter_by(id=data.invoice_id)
                      .with_for_update().first())
            if invoice is None or invoice.organization_id != organization_id:
                raise InvoiceNotFoundError(data.invoice_id)
            payment = _record_payment_on_invoice(db, organization_id, invoice, data, recorded_by)
            return _payment_to_out(payment)

    def finalize_new_bill(self, organization_id: uuid.UUID, sales_order_id: uuid.UUID,
                         actor_id: uuid.UUID, data: FinalizeSaleRequest) -> FinalizeSaleResult:
        """The "New Bill: Save as Sale" action — confirm + fulfill + generate
        the invoice, and optionally record a payment, as ONE database
        transaction. If any step fails (insufficient stock, a concurrent
        duplicate invoice, an over-large payment), everything rolls back:
        the sales order is left exactly as it was before this call, no
        stock is deducted, no invoice/payment row exists. Only a DRAFT
        order can be finalized this way — the same rule
        SalesService.finalize_new_bill already checks before calling
        here, re-checked here as defense in depth, same convention as
        fulfill_sale/generate_invoice re-checking their own preconditions.
        """
        with get_session() as db:
            so = db.get(SalesOrder, sales_order_id, with_for_update=True)
            if so is None or so.organization_id != organization_id:
                raise SalesOrderNotFoundError(sales_order_id)
            if so.status != SalesOrderStatus.DRAFT:
                raise InvalidSalesOrderTransitionError(so.status, SalesOrderStatus.CONFIRMED)

            _confirm(db, so, actor_id)
            _fulfill(db, organization_id, so, actor_id, write_audit=False)
            invoice = _generate_invoice(
                db, organization_id, so, actor_id, due_date=data.due_date,
                overall_discount_amount=data.overall_discount_amount,
                other_charges=data.other_charges, write_audit=False)

            payment = None
            if data.payment_amount is not None:
                payment_request = PaymentRequest(
                    invoice_id=invoice.id, amount=data.payment_amount,
                    method=data.payment_method, notes=data.payment_notes)
                payment = _record_payment_on_invoice(
                    db, organization_id, invoice, payment_request, actor_id, write_audit=False)

            actor_email, org_name = _audit_actor(db, organization_id, actor_id)
            record_audit_log(
                db, organization_id=organization_id, user_id=actor_id,
                actor_email=actor_email, organization_name=org_name,
                action="sales_order.new_bill_finalized", entity_type="sales_order",
                entity_id=so.id,
                changes={"invoice_number": invoice.invoice_number,
                        "total_amount": str(invoice.total_amount),
                        "payment_amount": str(payment.amount) if payment else None})
            db.flush()

            return FinalizeSaleResult(sales_order=_to_out(so), invoice=_invoice_to_out(invoice),
                                      payment=_payment_to_out(payment) if payment else None)

    def record_return(self, organization_id: uuid.UUID, sales_order_id: uuid.UUID,
                      sales_order_item_id: uuid.UUID, quantity: Decimal, reason: str,
                      returned_by: uuid.UUID) -> SalesReturnOut:
        with get_session() as db:
            so = db.get(SalesOrder, sales_order_id)
            if so is None or so.organization_id != organization_id:
                raise SalesOrderNotFoundError(sales_order_id)

            item = (db.query(SalesOrderItem)
                   .filter_by(id=sales_order_item_id, sales_order_id=so.id)
                   .with_for_update()
                   .first())
            if item is None:
                raise SalesOrderItemNotFoundError(sales_order_item_id)
            if quantity <= 0:
                raise SalesOrderValidationError(["Quantity must be greater than zero."])
            if quantity > item.quantity_fulfilled:
                raise SalesOrderValidationError(
                    [f"Cannot return {quantity} — only {item.quantity_fulfilled} currently "
                     "fulfilled from this order line."])

            item.quantity_fulfilled = item.quantity_fulfilled - quantity

            tx = _apply(db, organization_id=organization_id, product_id=item.product_id,
                       warehouse_id=so.warehouse_id,
                       transaction_type=InventoryTransactionType.RETURN_IN,
                       quantity_change=quantity, performed_by=returned_by,
                       reference_type="sales_order_item", reference_id=item.id, notes=reason)

            # Priced with the same formula the line was originally invoiced
            # with — not a fresh price lookup — so a return of part of a
            # discounted/taxed line credits back exactly its share, no
            # more, no less.
            credit_amount = line_total_after_discount(
                quantity, item.unit_price, item.discount_percent, item.tax_percent,
                excise_percent=item.excise_percent)

            sales_return = SalesReturn(
                organization_id=organization_id, sales_order_id=so.id,
                sales_order_item_id=item.id, warehouse_id=so.warehouse_id,
                product_id=item.product_id, quantity=quantity, reason=reason,
                returned_by=returned_by, inventory_transaction_id=tx.id,
                credit_amount=credit_amount)
            db.add(sales_return)
            db.flush()

            actor_email, org_name = _audit_actor(db, organization_id, returned_by)
            record_audit_log(
                db, organization_id=organization_id, user_id=returned_by,
                actor_email=actor_email, organization_name=org_name,
                action="sales_order.record_return", entity_type="sales_order",
                entity_id=so.id,
                changes={"sales_order_item_id": str(item.id), "quantity": str(quantity),
                        "reason": reason})

            db.flush()
            return _return_to_out(sales_return)
