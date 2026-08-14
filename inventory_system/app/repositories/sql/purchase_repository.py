"""SQLAlchemy-backed PurchaseOrderRepository.

receive_goods and record_return are the two methods that must be truly
atomic across three concerns at once: the inventory ledger, the purchase
order's own state (PurchaseOrderItem.quantity_received, PurchaseOrder.
status), and the audit trail. Rather than calling SqlInventoryRepository
(which opens its own, separate get_session()/transaction — no different
from calling it over the network), both methods reuse
app.repositories.sql.inventory_repository's module-level ``_apply`` helper
directly, inside their own ``with get_session()`` block, exactly the way
SqlInventoryRepository.transfer composes two ``_apply`` calls into one
transaction. If any step fails — an over-receipt, an insufficient-stock
guard tripping, a constraint violation — the whole receipt or return rolls
back: no half-updated inventory, no orphaned ledger row, no purchase order
left claiming a status its item quantities don't support.

Row locking: PurchaseOrderItem rows touched by receive_goods/record_return
are ``SELECT ... FOR UPDATE``'d before their quantity_received is read or
written, so two concurrent receipts (or a receipt racing a return) against
the same line item can't lose an update to each other — the same pattern
_apply already uses for Inventory rows.
"""
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.exc import IntegrityError

from app.core.exceptions import (
    InvalidPurchaseOrderTransitionError,
    PurchaseOrderItemNotFoundError,
    PurchaseOrderNotFoundError,
    PurchaseOrderValidationError,
)
from app.database.session import get_session
from app.domain.inventory import InventoryTransactionType
from app.domain.purchasing import PurchaseOrderStatus, format_purchase_order_number
from app.models import (
    GoodsReceipt,
    GoodsReceiptItem,
    Organization,
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseOrderSequence,
    PurchaseReturn,
    User,
)
from app.repositories.sql.audit_log_repository import record_audit_log
from app.repositories.sql.inventory_repository import _apply
from app.schemas.purchasing import (
    GoodsReceiptItemOut,
    GoodsReceiptLineInput,
    GoodsReceiptOut,
    PurchaseOrderCreate,
    PurchaseOrderFilter,
    PurchaseOrderItemOut,
    PurchaseOrderOut,
    PurchaseOrderPage,
    PurchaseOrderUpdate,
    PurchaseReturnOut,
)


def _item_to_out(item: PurchaseOrderItem) -> PurchaseOrderItemOut:
    return PurchaseOrderItemOut(id=item.id, product_id=item.product_id,
                                quantity_ordered=item.quantity_ordered,
                                quantity_received=item.quantity_received,
                                unit_price=item.unit_price, tax_percent=item.tax_percent)


def _to_out(po: PurchaseOrder) -> PurchaseOrderOut:
    return PurchaseOrderOut(
        id=po.id, order_number=po.order_number, supplier_id=po.supplier_id,
        warehouse_id=po.warehouse_id, status=po.status,
        expected_date=po.expected_date, notes=po.notes, created_by=po.created_by,
        approved_by=po.approved_by, approved_at=po.approved_at,
        items=[_item_to_out(i) for i in po.items], created_at=po.created_at,
        updated_at=po.updated_at)


def _lock_or_create_purchase_order_sequence(db, organization_id: uuid.UUID
                                            ) -> PurchaseOrderSequence:
    seq = (db.query(PurchaseOrderSequence).filter_by(organization_id=organization_id)
          .with_for_update().first())
    if seq is not None:
        return seq

    # First purchase order ever created for this organization — same
    # SAVEPOINT-protected create as
    # sales_repository._lock_or_create_invoice_sequence, for the same
    # concurrent-first-write race.
    savepoint = db.begin_nested()
    try:
        seq = PurchaseOrderSequence(organization_id=organization_id, next_value=1)
        db.add(seq)
        db.flush()
        savepoint.commit()
        return seq
    except IntegrityError:
        savepoint.rollback()
        seq = (db.query(PurchaseOrderSequence).filter_by(organization_id=organization_id)
              .with_for_update().first())
        if seq is None:
            raise
        return seq


def _receipt_to_out(receipt: GoodsReceipt) -> GoodsReceiptOut:
    return GoodsReceiptOut(
        id=receipt.id, purchase_order_id=receipt.purchase_order_id,
        warehouse_id=receipt.warehouse_id, received_by=receipt.received_by,
        notes=receipt.notes, received_at=receipt.received_at,
        items=[GoodsReceiptItemOut(id=i.id, purchase_order_item_id=i.purchase_order_item_id,
                                   quantity_received=i.quantity_received,
                                   inventory_transaction_id=i.inventory_transaction_id)
              for i in receipt.items])


def _return_to_out(r: PurchaseReturn) -> PurchaseReturnOut:
    return PurchaseReturnOut(id=r.id, purchase_order_id=r.purchase_order_id,
                             purchase_order_item_id=r.purchase_order_item_id,
                             warehouse_id=r.warehouse_id, product_id=r.product_id,
                             quantity=r.quantity, reason=r.reason, returned_by=r.returned_by,
                             inventory_transaction_id=r.inventory_transaction_id,
                             returned_at=r.returned_at)


class SqlPurchaseOrderRepository:
    def create(self, organization_id: uuid.UUID, data: PurchaseOrderCreate,
              created_by: uuid.UUID) -> PurchaseOrderOut:
        with get_session() as db:
            org = db.get(Organization, organization_id)
            seq = _lock_or_create_purchase_order_sequence(db, organization_id)
            order_number = format_purchase_order_number(org.purchase_number_prefix,
                                                         seq.next_value)
            seq.next_value += 1

            po = PurchaseOrder(organization_id=organization_id, order_number=order_number,
                               supplier_id=data.supplier_id,
                               warehouse_id=data.warehouse_id,
                               status=PurchaseOrderStatus.DRAFT,
                               expected_date=data.expected_date, notes=data.notes,
                               created_by=created_by)
            db.add(po)
            db.flush()
            for item in data.items:
                db.add(PurchaseOrderItem(purchase_order_id=po.id, product_id=item.product_id,
                                        quantity_ordered=item.quantity_ordered,
                                        unit_price=item.unit_price,
                                        tax_percent=item.tax_percent))
            db.flush()
            return _to_out(po)

    def update(self, organization_id: uuid.UUID, purchase_order_id: uuid.UUID,
              data: PurchaseOrderUpdate) -> PurchaseOrderOut | None:
        with get_session() as db:
            po = db.get(PurchaseOrder, purchase_order_id)
            if po is None or po.organization_id != organization_id:
                return None
            for field, value in data.model_dump(exclude_unset=True, exclude={"items"}).items():
                setattr(po, field, value)
            if data.items is not None:
                for existing in list(po.items):
                    db.delete(existing)
                db.flush()
                for item in data.items:
                    db.add(PurchaseOrderItem(purchase_order_id=po.id,
                                            product_id=item.product_id,
                                            quantity_ordered=item.quantity_ordered,
                                            unit_price=item.unit_price,
                                            tax_percent=item.tax_percent))
                db.flush()
                # The new items were added directly (not via po.items.append),
                # so the already-loaded po.items collection is still the
                # stale, pre-replacement list at this point — expire it so
                # _to_out(po) below reloads it fresh from the database
                # instead of returning items that no longer exist.
                db.expire(po, ["items"])
            return _to_out(po)

    def get_by_id(self, organization_id: uuid.UUID,
                 purchase_order_id: uuid.UUID) -> PurchaseOrderOut | None:
        with get_session() as db:
            po = db.get(PurchaseOrder, purchase_order_id)
            if po is None or po.organization_id != organization_id:
                return None
            return _to_out(po)

    def search(self, organization_id: uuid.UUID,
              filter: PurchaseOrderFilter) -> PurchaseOrderPage:
        with get_session() as db:
            query = db.query(PurchaseOrder).filter(
                PurchaseOrder.organization_id == organization_id)
            if filter.supplier_id:
                query = query.filter(PurchaseOrder.supplier_id == filter.supplier_id)
            if filter.status:
                query = query.filter(PurchaseOrder.status == filter.status)

            total = query.count()
            query = query.order_by(PurchaseOrder.created_at.desc())

            page = max(1, filter.page)
            page_size = max(1, filter.page_size)
            rows = query.offset((page - 1) * page_size).limit(page_size).all()

            return PurchaseOrderPage(items=[_to_out(p) for p in rows], total=total, page=page,
                                     page_size=page_size)

    def _transition(self, organization_id: uuid.UUID, purchase_order_id: uuid.UUID,
                    target: PurchaseOrderStatus) -> PurchaseOrderOut | None:
        with get_session() as db:
            po = db.get(PurchaseOrder, purchase_order_id)
            if po is None or po.organization_id != organization_id:
                return None
            po.status = target
            db.flush()
            return _to_out(po)

    def submit(self, organization_id: uuid.UUID,
              purchase_order_id: uuid.UUID) -> PurchaseOrderOut | None:
        return self._transition(organization_id, purchase_order_id,
                                PurchaseOrderStatus.SUBMITTED)

    def approve(self, organization_id: uuid.UUID, purchase_order_id: uuid.UUID,
              approved_by: uuid.UUID) -> PurchaseOrderOut | None:
        with get_session() as db:
            po = db.get(PurchaseOrder, purchase_order_id)
            if po is None or po.organization_id != organization_id:
                return None
            previous_status = po.status
            po.status = PurchaseOrderStatus.APPROVED
            po.approved_by = approved_by
            po.approved_at = datetime.now(timezone.utc)
            db.flush()

            user = db.get(User, approved_by)
            org = db.get(Organization, organization_id)
            record_audit_log(
                db, organization_id=organization_id, user_id=approved_by,
                actor_email=user.email if user else None,
                organization_name=org.name if org else None,
                action="purchase_order.approve", entity_type="purchase_order", entity_id=po.id,
                changes={"before": {"status": previous_status.value},
                        "after": {"status": PurchaseOrderStatus.APPROVED.value}})

            db.flush()
            return _to_out(po)

    def cancel(self, organization_id: uuid.UUID, purchase_order_id: uuid.UUID,
              cancelled_by: uuid.UUID) -> PurchaseOrderOut | None:
        with get_session() as db:
            po = db.get(PurchaseOrder, purchase_order_id)
            if po is None or po.organization_id != organization_id:
                return None
            previous_status = po.status
            po.status = PurchaseOrderStatus.CANCELLED
            db.flush()

            user = db.get(User, cancelled_by)
            org = db.get(Organization, organization_id)
            record_audit_log(
                db, organization_id=organization_id, user_id=cancelled_by,
                actor_email=user.email if user else None,
                organization_name=org.name if org else None,
                action="purchase_order.cancel", entity_type="purchase_order", entity_id=po.id,
                changes={"before": {"status": previous_status.value},
                        "after": {"status": PurchaseOrderStatus.CANCELLED.value}})

            db.flush()
            return _to_out(po)

    def receive_goods(self, organization_id: uuid.UUID, purchase_order_id: uuid.UUID,
                      lines: list[GoodsReceiptLineInput], received_by: uuid.UUID,
                      notes: str | None = None) -> GoodsReceiptOut:
        with get_session() as db:
            po = db.get(PurchaseOrder, purchase_order_id)
            if po is None or po.organization_id != organization_id:
                raise PurchaseOrderNotFoundError(purchase_order_id)
            if po.status not in (PurchaseOrderStatus.APPROVED,
                                PurchaseOrderStatus.PARTIALLY_RECEIVED):
                raise InvalidPurchaseOrderTransitionError(
                    po.status, PurchaseOrderStatus.PARTIALLY_RECEIVED)

            receipt = GoodsReceipt(organization_id=organization_id,
                                   purchase_order_id=po.id, warehouse_id=po.warehouse_id,
                                   received_by=received_by, notes=notes)
            db.add(receipt)
            db.flush()

            for line in lines:
                item = (db.query(PurchaseOrderItem)
                       .filter_by(id=line.purchase_order_item_id, purchase_order_id=po.id)
                       .with_for_update()
                       .first())
                if item is None:
                    raise PurchaseOrderItemNotFoundError(line.purchase_order_item_id)
                if line.quantity <= 0:
                    raise PurchaseOrderValidationError(
                        ["Quantity received must be greater than zero."])
                new_received = item.quantity_received + line.quantity
                if new_received > item.quantity_ordered:
                    outstanding = item.quantity_ordered - item.quantity_received
                    raise PurchaseOrderValidationError(
                        [f"Cannot receive {line.quantity} for item {item.id} — only "
                         f"{outstanding} outstanding."])
                item.quantity_received = new_received

                tx = _apply(db, organization_id=organization_id, product_id=item.product_id,
                           warehouse_id=po.warehouse_id,
                           transaction_type=InventoryTransactionType.PURCHASE_RECEIVED,
                           quantity_change=line.quantity, performed_by=received_by,
                           reference_type="purchase_order_item", reference_id=item.id,
                           notes=notes)

                db.add(GoodsReceiptItem(goods_receipt_id=receipt.id,
                                       purchase_order_item_id=item.id,
                                       quantity_received=line.quantity,
                                       inventory_transaction_id=tx.id))

            db.flush()

            if all(i.quantity_received >= i.quantity_ordered for i in po.items):
                po.status = PurchaseOrderStatus.RECEIVED
            else:
                po.status = PurchaseOrderStatus.PARTIALLY_RECEIVED

            user = db.get(User, received_by)
            org = db.get(Organization, organization_id)
            record_audit_log(
                db, organization_id=organization_id, user_id=received_by,
                actor_email=user.email if user else None,
                organization_name=org.name if org else None,
                action="purchase_order.receive_goods", entity_type="purchase_order",
                entity_id=po.id,
                changes={"lines": [{"purchase_order_item_id": str(line.purchase_order_item_id),
                                    "quantity": str(line.quantity)} for line in lines],
                        "resulting_status": po.status.value})

            db.flush()
            return _receipt_to_out(receipt)

    def record_return(self, organization_id: uuid.UUID, purchase_order_id: uuid.UUID,
                      purchase_order_item_id: uuid.UUID, quantity: Decimal, reason: str,
                      returned_by: uuid.UUID) -> PurchaseReturnOut:
        with get_session() as db:
            po = db.get(PurchaseOrder, purchase_order_id)
            if po is None or po.organization_id != organization_id:
                raise PurchaseOrderNotFoundError(purchase_order_id)

            item = (db.query(PurchaseOrderItem)
                   .filter_by(id=purchase_order_item_id, purchase_order_id=po.id)
                   .with_for_update()
                   .first())
            if item is None:
                raise PurchaseOrderItemNotFoundError(purchase_order_item_id)
            if quantity <= 0:
                raise PurchaseOrderValidationError(["Quantity must be greater than zero."])
            if quantity > item.quantity_received:
                raise PurchaseOrderValidationError(
                    [f"Cannot return {quantity} — only {item.quantity_received} currently "
                     "held from this order line."])

            item.quantity_received = item.quantity_received - quantity

            tx = _apply(db, organization_id=organization_id, product_id=item.product_id,
                       warehouse_id=po.warehouse_id,
                       transaction_type=InventoryTransactionType.RETURN_OUT,
                       quantity_change=-quantity, performed_by=returned_by,
                       reference_type="purchase_order_item", reference_id=item.id,
                       notes=reason)

            purchase_return = PurchaseReturn(
                organization_id=organization_id, purchase_order_id=po.id,
                purchase_order_item_id=item.id, warehouse_id=po.warehouse_id,
                product_id=item.product_id, quantity=quantity, reason=reason,
                returned_by=returned_by, inventory_transaction_id=tx.id)
            db.add(purchase_return)
            db.flush()

            user = db.get(User, returned_by)
            org = db.get(Organization, organization_id)
            record_audit_log(
                db, organization_id=organization_id, user_id=returned_by,
                actor_email=user.email if user else None,
                organization_name=org.name if org else None,
                action="purchase_order.record_return", entity_type="purchase_order",
                entity_id=po.id,
                changes={"purchase_order_item_id": str(item.id), "quantity": str(quantity),
                        "reason": reason})

            db.flush()
            return _return_to_out(purchase_return)
