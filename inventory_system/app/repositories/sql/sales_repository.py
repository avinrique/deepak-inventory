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
"""
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from app.core.exceptions import (
    DuplicateInvoiceError,
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
from app.repositories.sql.audit_log_repository import record_audit_log
from app.repositories.sql.inventory_repository import _apply
from app.schemas.sales import (
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
                             discount_percent=item.discount_percent)


def _to_out(so: SalesOrder) -> SalesOrderOut:
    return SalesOrderOut(
        id=so.id, customer_id=so.customer_id, warehouse_id=so.warehouse_id, status=so.status,
        notes=so.notes, created_by=so.created_by, confirmed_by=so.confirmed_by,
        confirmed_at=so.confirmed_at, items=[_item_to_out(i) for i in so.items],
        created_at=so.created_at, updated_at=so.updated_at)


def _invoice_to_out(invoice: Invoice) -> InvoiceOut:
    return InvoiceOut(id=invoice.id, sales_order_id=invoice.sales_order_id,
                      invoice_number=invoice.invoice_number, subtotal=invoice.subtotal,
                      discount_amount=invoice.discount_amount, tax_amount=invoice.tax_amount,
                      total_amount=invoice.total_amount, generated_by=invoice.generated_by,
                      generated_at=invoice.generated_at)


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
                          returned_at=r.returned_at)


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


class SqlSalesOrderRepository:
    def create(self, organization_id: uuid.UUID, data: SalesOrderCreate,
              created_by: uuid.UUID) -> SalesOrderOut:
        with get_session() as db:
            so = SalesOrder(organization_id=organization_id, customer_id=data.customer_id,
                            warehouse_id=data.warehouse_id, status=SalesOrderStatus.DRAFT,
                            notes=data.notes, created_by=created_by)
            db.add(so)
            db.flush()
            for item in data.items:
                db.add(SalesOrderItem(sales_order_id=so.id, product_id=item.product_id,
                                     quantity_ordered=item.quantity_ordered,
                                     unit_price=item.unit_price,
                                     tax_percent=item.tax_percent,
                                     discount_percent=item.discount_percent))
            db.flush()
            return _to_out(so)

    def update(self, organization_id: uuid.UUID, sales_order_id: uuid.UUID,
              data: SalesOrderUpdate) -> SalesOrderOut | None:
        with get_session() as db:
            so = db.get(SalesOrder, sales_order_id)
            if so is None or so.organization_id != organization_id:
                return None
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
                                         tax_percent=item.tax_percent))
                db.flush()
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

    def search(self, organization_id: uuid.UUID,
              filter: SalesOrderFilter) -> SalesOrderPage:
        with get_session() as db:
            query = db.query(SalesOrder).filter(SalesOrder.organization_id == organization_id)
            if filter.customer_id:
                query = query.filter(SalesOrder.customer_id == filter.customer_id)
            if filter.status:
                query = query.filter(SalesOrder.status == filter.status)

            total = query.count()
            query = query.order_by(SalesOrder.created_at.desc())

            page = max(1, filter.page)
            page_size = max(1, filter.page_size)
            rows = query.offset((page - 1) * page_size).limit(page_size).all()

            return SalesOrderPage(items=[_to_out(s) for s in rows], total=total, page=page,
                                  page_size=page_size)

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
            so = db.get(SalesOrder, sales_order_id)
            if so is None or so.organization_id != organization_id:
                return None
            so.status = SalesOrderStatus.CONFIRMED
            so.confirmed_by = confirmed_by
            so.confirmed_at = datetime.now(timezone.utc)
            db.flush()
            return _to_out(so)

    def cancel(self, organization_id: uuid.UUID, sales_order_id: uuid.UUID,
              cancelled_by: uuid.UUID) -> SalesOrderOut | None:
        with get_session() as db:
            so = db.get(SalesOrder, sales_order_id)
            if so is None or so.organization_id != organization_id:
                return None
            previous_status = so.status
            so.status = SalesOrderStatus.CANCELLED
            db.flush()

            user = db.get(User, cancelled_by)
            org = db.get(Organization, organization_id)
            record_audit_log(
                db, organization_id=organization_id, user_id=cancelled_by,
                actor_email=user.email if user else None,
                organization_name=org.name if org else None,
                action="sales_order.cancel", entity_type="sales_order", entity_id=so.id,
                changes={"before": {"status": previous_status.value},
                        "after": {"status": SalesOrderStatus.CANCELLED.value}})

            db.flush()
            return _to_out(so)

    def fulfill_sale(self, organization_id: uuid.UUID, sales_order_id: uuid.UUID,
                    fulfilled_by: uuid.UUID) -> SalesOrderOut:
        with get_session() as db:
            so = db.get(SalesOrder, sales_order_id)
            if so is None or so.organization_id != organization_id:
                raise SalesOrderNotFoundError(sales_order_id)
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

            user = db.get(User, fulfilled_by)
            org = db.get(Organization, organization_id)
            record_audit_log(
                db, organization_id=organization_id, user_id=fulfilled_by,
                actor_email=user.email if user else None,
                organization_name=org.name if org else None,
                action="sales_order.fulfill", entity_type="sales_order", entity_id=so.id,
                changes={"items": [{"product_id": str(i.product_id),
                                    "quantity": str(i.quantity_ordered)} for i in items]})

            db.flush()
            return _to_out(so)

    def generate_invoice(self, organization_id: uuid.UUID, sales_order_id: uuid.UUID,
                        generated_by: uuid.UUID) -> InvoiceOut:
        with get_session() as db:
            so = db.get(SalesOrder, sales_order_id)
            if so is None or so.organization_id != organization_id:
                raise SalesOrderNotFoundError(sales_order_id)
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
            total_amount = sum((line_total_after_discount(i.quantity_ordered, i.unit_price,
                                                           i.discount_percent, i.tax_percent)
                               for i in so.items), Decimal("0"))

            org = db.get(Organization, organization_id)
            seq = _lock_or_create_invoice_sequence(db, organization_id)
            number = format_invoice_number(org.invoice_number_prefix, seq.next_value)
            seq.next_value += 1

            invoice = Invoice(organization_id=organization_id, sales_order_id=so.id,
                             invoice_number=number, subtotal=subtotal,
                             discount_amount=discount_amount, tax_amount=tax_amount,
                             total_amount=total_amount, generated_by=generated_by)
            db.add(invoice)
            db.flush()

            user = db.get(User, generated_by)
            record_audit_log(
                db, organization_id=organization_id, user_id=generated_by,
                actor_email=user.email if user else None,
                organization_name=org.name if org else None,
                action="sales_order.generate_invoice", entity_type="invoice",
                entity_id=invoice.id,
                changes={"sales_order_id": str(so.id), "invoice_number": number,
                        "total_amount": str(total_amount)})

            db.flush()
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
                total = line_total_after_discount(item.quantity_ordered, item.unit_price,
                                                  item.discount_percent, item.tax_percent)
                lines.append(InvoiceDocumentLine(
                    sku=product.sku, product_name=product.name, quantity=item.quantity_ordered,
                    unit_price=item.unit_price, discount_percent=item.discount_percent,
                    tax_percent=item.tax_percent, line_subtotal=subtotal, line_discount=discount,
                    line_tax=tax, line_total=total))

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
                invoice_date=invoice.generated_at, sales_order_id=so.id,
                customer_name=customer.name, customer_address=customer.address,
                customer_phone=customer.phone, customer_email=customer.email,
                customer_tax_id=customer.tax_id,
                items=lines, subtotal=invoice.subtotal, discount_total=invoice.discount_amount,
                tax_total=invoice.tax_amount, total=invoice.total_amount, amount_paid=paid,
                amount_due=amount_due, payment_status=status, notes=so.notes)

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

            user = db.get(User, recorded_by)
            org = db.get(Organization, organization_id)
            record_audit_log(
                db, organization_id=organization_id, user_id=recorded_by,
                actor_email=user.email if user else None,
                organization_name=org.name if org else None,
                action="sales_order.record_payment", entity_type="invoice",
                entity_id=invoice.id,
                changes={"amount": str(data.amount), "method": data.method.value,
                        "total_paid": str(paid_sum)})

            db.flush()
            return _payment_to_out(payment)

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

            sales_return = SalesReturn(
                organization_id=organization_id, sales_order_id=so.id,
                sales_order_item_id=item.id, warehouse_id=so.warehouse_id,
                product_id=item.product_id, quantity=quantity, reason=reason,
                returned_by=returned_by, inventory_transaction_id=tx.id)
            db.add(sales_return)
            db.flush()

            user = db.get(User, returned_by)
            org = db.get(Organization, organization_id)
            record_audit_log(
                db, organization_id=organization_id, user_id=returned_by,
                actor_email=user.email if user else None,
                organization_name=org.name if org else None,
                action="sales_order.record_return", entity_type="sales_order",
                entity_id=so.id,
                changes={"sales_order_item_id": str(item.id), "quantity": str(quantity),
                        "reason": reason})

            db.flush()
            return _return_to_out(sales_return)
