"""SQLAlchemy-backed InventoryRepository — the one place that mutates
Inventory (the current-quantity snapshot) and appends to
InventoryTransaction (the immutable ledger) together, atomically, under a
row lock. Every public method here corresponds to one of the operations
the caller thinks in (Stock In, Stock Out, Adjustment, Transfer, ...); all
of them funnel through ``_apply``, so locking/validation/audit-trail
behavior lives in exactly one place rather than being re-derived per
operation.

Concurrency: ``SELECT ... FOR UPDATE`` (via SQLAlchemy's
``with_for_update()``) on the Inventory row for a given (product,
warehouse) pair serializes concurrent writers — a second transaction
touching the same row blocks until the first commits or rolls back, so two
concurrent "sell 5 units" calls against 5 units on hand can't both
succeed (lost-update prevention). ``transfer`` locks both the source and
destination rows *in a canonical order* (sorted by warehouse_id) before
touching either, so two concurrent transfers moving stock in opposite
directions between the same two warehouses can't deadlock each other by
acquiring those two locks in reverse order.

Negative-stock policy (Organization.allow_negative_stock) is read inside
the same locked transaction as the update it gates, not fetched
separately beforehand — so there's no window where the flag could change
between checking it and applying the change.
"""
import uuid
from decimal import Decimal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import (
    InsufficientStockError,
    InvalidTransferError,
    InventoryValidationError,
    LowStockBlockedError,
)
from app.database.session import get_session
from app.domain.inventory import RESERVED_TYPES, InventoryTransactionType, LowStockBehavior
from app.models import (
    Inventory,
    InventoryTransaction,
    Organization,
    Product,
    StockAdjustment,
    StockTransfer,
    User,
    Warehouse,
)
from app.repositories.sql.audit_log_repository import record_audit_log
from app.schemas.inventory import InventoryLevel, InventoryTransactionOut, InventoryTransactionPage, TransactionFilter


def _to_tx_out(tx: InventoryTransaction) -> InventoryTransactionOut:
    return InventoryTransactionOut(
        id=tx.id, product_id=tx.product_id, warehouse_id=tx.warehouse_id,
        transaction_type=tx.transaction_type, quantity_change=tx.quantity_change,
        quantity_on_hand_after=tx.quantity_on_hand_after,
        quantity_reserved_after=tx.quantity_reserved_after, reference_type=tx.reference_type,
        reference_id=tx.reference_id, performed_by=tx.performed_by, notes=tx.notes,
        created_at=tx.created_at)


def _lock_or_create_inventory_row(db: Session, organization_id: uuid.UUID,
                                  product_id: uuid.UUID, warehouse_id: uuid.UUID) -> Inventory:
    row = (db.query(Inventory)
           .filter_by(organization_id=organization_id, product_id=product_id,
                     warehouse_id=warehouse_id)
           .with_for_update()
           .first())
    if row is not None:
        return row

    # No row yet for this (product, warehouse) pair — this is the first
    # stock event ever recorded for it. Insert under a SAVEPOINT: if a
    # concurrent transaction is doing the exact same insert at the same
    # moment, the unique index on (organization_id, product_id,
    # warehouse_id) makes one of the two INSERTs fail with a unique
    # violation once the other commits. Postgres aborts the whole
    # transaction on a constraint violation unless it happened inside a
    # SAVEPOINT — so without this, the loser here would take down its
    # entire enclosing get_session() transaction, not just this insert.
    savepoint = db.begin_nested()
    try:
        row = Inventory(organization_id=organization_id, product_id=product_id,
                        warehouse_id=warehouse_id, quantity_on_hand=Decimal("0"),
                        quantity_reserved=Decimal("0"))
        db.add(row)
        db.flush()
        savepoint.commit()
        return row
    except IntegrityError:
        savepoint.rollback()
        row = (db.query(Inventory)
               .filter_by(organization_id=organization_id, product_id=product_id,
                         warehouse_id=warehouse_id)
               .with_for_update()
               .first())
        if row is None:
            raise
        return row


def _apply(db: Session, *, organization_id: uuid.UUID, product_id: uuid.UUID,
          warehouse_id: uuid.UUID, transaction_type: InventoryTransactionType,
          quantity_change: Decimal, performed_by: uuid.UUID,
          reference_type: str | None = None, reference_id: uuid.UUID | None = None,
          notes: str | None = None, enforce_low_stock_policy: bool = False
          ) -> InventoryTransaction:
    row = _lock_or_create_inventory_row(db, organization_id, product_id, warehouse_id)

    if transaction_type in RESERVED_TYPES:
        new_reserved = row.quantity_reserved + quantity_change
        if new_reserved < 0:
            raise InventoryValidationError(["Cannot release more than is currently reserved."])
        if new_reserved > row.quantity_on_hand:
            raise InsufficientStockError(product_id, warehouse_id,
                                         row.quantity_on_hand - row.quantity_reserved,
                                         quantity_change)
        row.quantity_reserved = new_reserved
    else:
        new_on_hand = row.quantity_on_hand + quantity_change
        if new_on_hand < 0:
            org = db.get(Organization, organization_id)
            if org is None or not org.allow_negative_stock:
                raise InsufficientStockError(product_id, warehouse_id,
                                             row.quantity_on_hand, -quantity_change)
        # enforce_low_stock_policy is only ever passed by
        # SalesOrderRepository.fulfill_sale — see app.domain.inventory.
        # LowStockBehavior's docstring for why manual adjustments/damage/
        # transfers deliberately never enforce this.
        if enforce_low_stock_policy and quantity_change < 0:
            org = db.get(Organization, organization_id)
            if org is not None and org.low_stock_behavior == LowStockBehavior.BLOCK_SALE:
                product = db.get(Product, product_id)
                if product is not None and new_on_hand < product.minimum_stock_level:
                    raise LowStockBlockedError(product_id, warehouse_id, new_on_hand,
                                               product.minimum_stock_level)
        # Reserved stock is a promise against physical quantity that
        # actually exists — on-hand dropping below it (e.g. an
        # organization with allow_negative_stock=True selling past
        # already-reserved stock) is still not something the app should
        # silently allow, but it's a rare edge case, not a primary
        # validation path: the ``ck_inventory_reserved_not_exceed_on_hand``
        # CHECK constraint on the inventory table catches it on flush, with
        # a raw IntegrityError rather than a friendly InsufficientStockError.
        row.quantity_on_hand = new_on_hand

    tx = InventoryTransaction(
        organization_id=organization_id, product_id=product_id, warehouse_id=warehouse_id,
        transaction_type=transaction_type, quantity_change=quantity_change,
        quantity_on_hand_after=row.quantity_on_hand,
        quantity_reserved_after=row.quantity_reserved, reference_type=reference_type,
        reference_id=reference_id, performed_by=performed_by, notes=notes)
    db.add(tx)
    db.flush()
    return tx


class SqlInventoryRepository:
    def stock_in(self, organization_id: uuid.UUID, product_id: uuid.UUID,
                warehouse_id: uuid.UUID, quantity: Decimal, performed_by: uuid.UUID,
                notes: str | None = None) -> InventoryTransactionOut:
        with get_session() as db:
            tx = _apply(db, organization_id=organization_id, product_id=product_id,
                       warehouse_id=warehouse_id, transaction_type=InventoryTransactionType.STOCK_IN,
                       quantity_change=quantity, performed_by=performed_by, notes=notes)
            return _to_tx_out(tx)

    def stock_out(self, organization_id: uuid.UUID, product_id: uuid.UUID,
                 warehouse_id: uuid.UUID, quantity: Decimal, performed_by: uuid.UUID,
                 notes: str | None = None) -> InventoryTransactionOut:
        with get_session() as db:
            tx = _apply(db, organization_id=organization_id, product_id=product_id,
                       warehouse_id=warehouse_id, transaction_type=InventoryTransactionType.STOCK_OUT,
                       quantity_change=-quantity, performed_by=performed_by, notes=notes)
            return _to_tx_out(tx)

    def mark_damaged(self, organization_id: uuid.UUID, product_id: uuid.UUID,
                     warehouse_id: uuid.UUID, quantity: Decimal, performed_by: uuid.UUID,
                     notes: str | None = None) -> InventoryTransactionOut:
        with get_session() as db:
            tx = _apply(db, organization_id=organization_id, product_id=product_id,
                       warehouse_id=warehouse_id, transaction_type=InventoryTransactionType.DAMAGE,
                       quantity_change=-quantity, performed_by=performed_by, notes=notes)
            return _to_tx_out(tx)

    def record_return(self, organization_id: uuid.UUID, product_id: uuid.UUID,
                      warehouse_id: uuid.UUID, quantity: Decimal, performed_by: uuid.UUID,
                      to_stock: bool, notes: str | None = None) -> InventoryTransactionOut:
        tx_type = (InventoryTransactionType.RETURN_IN if to_stock
                  else InventoryTransactionType.RETURN_OUT)
        signed = quantity if to_stock else -quantity
        with get_session() as db:
            tx = _apply(db, organization_id=organization_id, product_id=product_id,
                       warehouse_id=warehouse_id, transaction_type=tx_type,
                       quantity_change=signed, performed_by=performed_by, notes=notes)
            return _to_tx_out(tx)

    def adjust(self, organization_id: uuid.UUID, product_id: uuid.UUID,
              warehouse_id: uuid.UUID, quantity_change: Decimal, reason: str,
              performed_by: uuid.UUID) -> InventoryTransactionOut:
        with get_session() as db:
            tx = _apply(db, organization_id=organization_id, product_id=product_id,
                       warehouse_id=warehouse_id, transaction_type=InventoryTransactionType.ADJUSTMENT,
                       quantity_change=quantity_change, performed_by=performed_by, notes=reason)
            adjustment = StockAdjustment(
                organization_id=organization_id, product_id=product_id,
                warehouse_id=warehouse_id, quantity_change=quantity_change, reason=reason,
                performed_by=performed_by, inventory_transaction_id=tx.id)
            db.add(adjustment)
            db.flush()

            user = db.get(User, performed_by)
            org = db.get(Organization, organization_id)
            record_audit_log(
                db, organization_id=organization_id, user_id=performed_by,
                actor_email=user.email if user else None,
                organization_name=org.name if org else None,
                action="inventory.adjustment", entity_type="product", entity_id=product_id,
                changes={"warehouse_id": str(warehouse_id), "quantity_change": str(quantity_change),
                        "reason": reason, "resulting_on_hand": str(tx.quantity_on_hand_after)})

            db.flush()
            return _to_tx_out(tx)

    def transfer(self, organization_id: uuid.UUID, product_id: uuid.UUID,
                from_warehouse_id: uuid.UUID, to_warehouse_id: uuid.UUID, quantity: Decimal,
                performed_by: uuid.UUID, notes: str | None = None
                ) -> tuple[InventoryTransactionOut, InventoryTransactionOut]:
        if from_warehouse_id == to_warehouse_id:
            raise InvalidTransferError("Source and destination warehouse must be different.")
        with get_session() as db:
            # Canonical lock order — see module docstring.
            for wh_id in sorted([from_warehouse_id, to_warehouse_id], key=str):
                _lock_or_create_inventory_row(db, organization_id, product_id, wh_id)

            out_tx = _apply(db, organization_id=organization_id, product_id=product_id,
                           warehouse_id=from_warehouse_id,
                           transaction_type=InventoryTransactionType.TRANSFER_OUT,
                           quantity_change=-quantity, performed_by=performed_by, notes=notes)
            in_tx = _apply(db, organization_id=organization_id, product_id=product_id,
                          warehouse_id=to_warehouse_id,
                          transaction_type=InventoryTransactionType.TRANSFER_IN,
                          quantity_change=quantity, performed_by=performed_by, notes=notes)
            transfer = StockTransfer(
                organization_id=organization_id, product_id=product_id,
                from_warehouse_id=from_warehouse_id, to_warehouse_id=to_warehouse_id,
                quantity=quantity, notes=notes, performed_by=performed_by,
                out_transaction_id=out_tx.id, in_transaction_id=in_tx.id)
            db.add(transfer)
            db.flush()

            user = db.get(User, performed_by)
            org = db.get(Organization, organization_id)
            record_audit_log(
                db, organization_id=organization_id, user_id=performed_by,
                actor_email=user.email if user else None,
                organization_name=org.name if org else None,
                action="inventory.transfer", entity_type="product", entity_id=product_id,
                changes={"from_warehouse_id": str(from_warehouse_id),
                        "to_warehouse_id": str(to_warehouse_id), "quantity": str(quantity)})

            db.flush()
            return _to_tx_out(out_tx), _to_tx_out(in_tx)

    def reserve(self, organization_id: uuid.UUID, product_id: uuid.UUID,
               warehouse_id: uuid.UUID, quantity: Decimal, performed_by: uuid.UUID,
               notes: str | None = None) -> InventoryTransactionOut:
        with get_session() as db:
            tx = _apply(db, organization_id=organization_id, product_id=product_id,
                       warehouse_id=warehouse_id, transaction_type=InventoryTransactionType.RESERVE,
                       quantity_change=quantity, performed_by=performed_by, notes=notes)
            return _to_tx_out(tx)

    def release_reservation(self, organization_id: uuid.UUID, product_id: uuid.UUID,
                            warehouse_id: uuid.UUID, quantity: Decimal, performed_by: uuid.UUID,
                            notes: str | None = None) -> InventoryTransactionOut:
        with get_session() as db:
            tx = _apply(db, organization_id=organization_id, product_id=product_id,
                       warehouse_id=warehouse_id,
                       transaction_type=InventoryTransactionType.RELEASE_RESERVE,
                       quantity_change=-quantity, performed_by=performed_by, notes=notes)
            return _to_tx_out(tx)

    def get_level(self, organization_id: uuid.UUID, product_id: uuid.UUID,
                 warehouse_id: uuid.UUID) -> InventoryLevel:
        with get_session() as db:
            row = (db.query(Inventory)
                   .filter_by(organization_id=organization_id, product_id=product_id,
                             warehouse_id=warehouse_id)
                   .first())
            warehouse = db.get(Warehouse, warehouse_id)
            code = warehouse.code if warehouse else ""
            if row is None:
                return InventoryLevel(product_id=product_id, warehouse_id=warehouse_id,
                                      warehouse_code=code, quantity_on_hand=Decimal("0"),
                                      quantity_reserved=Decimal("0"))
            return InventoryLevel(product_id=product_id, warehouse_id=warehouse_id,
                                  warehouse_code=code, quantity_on_hand=row.quantity_on_hand,
                                  quantity_reserved=row.quantity_reserved)

    def get_levels_for_products(self, organization_id: uuid.UUID,
                                product_ids: list[uuid.UUID],
                                warehouse_id: uuid.UUID) -> dict[uuid.UUID, InventoryLevel]:
        """One (product, warehouse) level per id in ``product_ids``, in a
        single query — the bulk counterpart to get_level, for callers that
        need stock for a whole result set (e.g. a product-suggestion
        dropdown) without firing one query per row. A product with no
        Inventory row at this warehouse yet gets a zero-quantity level
        rather than being omitted, same as get_level's own fallback.
        """
        if not product_ids:
            return {}
        with get_session() as db:
            warehouse = db.get(Warehouse, warehouse_id)
            code = warehouse.code if warehouse else ""
            rows = (db.query(Inventory)
                   .filter(Inventory.organization_id == organization_id,
                          Inventory.warehouse_id == warehouse_id,
                          Inventory.product_id.in_(product_ids))
                   .all())
            levels = {r.product_id: InventoryLevel(
                product_id=r.product_id, warehouse_id=warehouse_id, warehouse_code=code,
                quantity_on_hand=r.quantity_on_hand, quantity_reserved=r.quantity_reserved)
                for r in rows}
            for product_id in product_ids:
                if product_id not in levels:
                    levels[product_id] = InventoryLevel(
                        product_id=product_id, warehouse_id=warehouse_id, warehouse_code=code,
                        quantity_on_hand=Decimal("0"), quantity_reserved=Decimal("0"))
            return levels

    def list_levels_for_product(self, organization_id: uuid.UUID,
                                product_id: uuid.UUID) -> list[InventoryLevel]:
        with get_session() as db:
            rows = (db.query(Inventory)
                   .filter_by(organization_id=organization_id, product_id=product_id)
                   .all())
            return [InventoryLevel(product_id=product_id, warehouse_id=r.warehouse_id,
                                   warehouse_code=r.warehouse.code,
                                   quantity_on_hand=r.quantity_on_hand,
                                   quantity_reserved=r.quantity_reserved)
                   for r in rows]

    def list_all_levels(self, organization_id: uuid.UUID) -> list[InventoryLevel]:
        with get_session() as db:
            rows = db.query(Inventory).filter_by(organization_id=organization_id).all()
            return [InventoryLevel(product_id=r.product_id, warehouse_id=r.warehouse_id,
                                   warehouse_code=r.warehouse.code,
                                   quantity_on_hand=r.quantity_on_hand,
                                   quantity_reserved=r.quantity_reserved)
                   for r in rows]

    def list_transactions(self, organization_id: uuid.UUID,
                          filter: TransactionFilter) -> InventoryTransactionPage:
        with get_session() as db:
            query = db.query(InventoryTransaction).filter(
                InventoryTransaction.organization_id == organization_id)
            if filter.product_id:
                query = query.filter(InventoryTransaction.product_id == filter.product_id)
            if filter.warehouse_id:
                query = query.filter(InventoryTransaction.warehouse_id == filter.warehouse_id)
            if filter.transaction_type:
                query = query.filter(
                    InventoryTransaction.transaction_type == filter.transaction_type)

            total = query.count()
            query = query.order_by(InventoryTransaction.created_at.desc())

            page = max(1, filter.page)
            page_size = max(1, filter.page_size)
            rows = query.offset((page - 1) * page_size).limit(page_size).all()

            return InventoryTransactionPage(items=[_to_tx_out(r) for r in rows], total=total,
                                            page=page, page_size=page_size)
