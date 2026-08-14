"""Inventory ledger — Inventory (current snapshot), InventoryTransaction
(immutable history), StockAdjustment and StockTransfer (records of *why* a
particular ledger entry, or entry pair, exists).

Current quantity is deliberately not the only source of truth: Inventory
rows are a cached snapshot, kept in sync transactionally by the one writer
that's allowed to touch them — InventoryRepository.apply_transaction (see
app.repositories.sql.inventory_repository) — every write also appends an
InventoryTransaction row in the same database transaction. Never
UPDATE/DELETE an InventoryTransaction row outside that path; it has no
updated_at and no repository method to change one on purpose because it
isn't supposed to happen — it's an append-only audit trail, and the
snapshot is recomputable from it if the two ever needed to be reconciled.
"""
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, DateTime, Enum, ForeignKey, Index, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.inventory import InventoryTransactionType
from app.models.base import Base, TimestampMixin, UUIDPKMixin


class Inventory(UUIDPKMixin, TimestampMixin, Base):
    """Current on-hand/reserved snapshot for one (product, warehouse) pair.
    Rows are created lazily, by the first transaction that touches a given
    pair (see InventoryRepository.apply_transaction) — not pre-seeded for
    every product/warehouse combination.
    """
    __tablename__ = "inventory"
    __table_args__ = (
        CheckConstraint("quantity_reserved >= 0",
                        name="ck_inventory_reserved_non_negative"),
        # Only enforced while on-hand is non-negative: once an organization
        # with allow_negative_stock=True has sold past zero, "reserved must
        # fit within on-hand" no longer has a coherent meaning to check —
        # relaxing it here (rather than dropping it) keeps the constraint
        # doing real work for every organization that hasn't opted into
        # negative stock, which is the common case.
        CheckConstraint("quantity_on_hand < 0 OR quantity_reserved <= quantity_on_hand",
                        name="ck_inventory_reserved_not_exceed_on_hand"),
        Index("ix_inventory_org_product_warehouse", "organization_id", "product_id",
             "warehouse_id", unique=True),
        Index("ix_inventory_warehouse_id", "warehouse_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False)
    # RESTRICT: a product/warehouse with inventory history can't be deleted
    # out from under it — same policy as Product.category_id/brand_id.
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id", ondelete="RESTRICT"), nullable=False)
    warehouse_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False)
    quantity_on_hand: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False,
                                                       default=Decimal("0"))
    quantity_reserved: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False,
                                                        default=Decimal("0"))

    product: Mapped["Product"] = relationship()
    warehouse: Mapped["Warehouse"] = relationship()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (f"Inventory(product_id={self.product_id!r}, "
               f"warehouse_id={self.warehouse_id!r}, on_hand={self.quantity_on_hand!r})")


class InventoryTransaction(UUIDPKMixin, Base):
    """Immutable ledger entry — the actual source of truth for how stock
    moved. No TimestampMixin: there is no updated_at, deliberately — a
    transaction row is never edited after it's written.
    """
    __tablename__ = "inventory_transactions"
    __table_args__ = (
        CheckConstraint("quantity_change != 0", name="ck_inventory_tx_quantity_nonzero"),
        Index("ix_inventory_tx_org_product_warehouse_created",
             "organization_id", "product_id", "warehouse_id", "created_at"),
        Index("ix_inventory_tx_org_type", "organization_id", "transaction_type"),
        Index("ix_inventory_tx_reference", "reference_type", "reference_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False)
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id", ondelete="RESTRICT"), nullable=False)
    warehouse_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False)
    transaction_type: Mapped[InventoryTransactionType] = mapped_column(
        Enum(InventoryTransactionType, name="inventory_transaction_type", native_enum=True),
        nullable=False)
    quantity_change: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False)
    quantity_on_hand_after: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False)
    quantity_reserved_after: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False)
    # Loosely-typed pointer at whatever business record caused this entry
    # (a StockAdjustment, a StockTransfer, and eventually a Sale/Purchase) —
    # no FK, since the referenced table varies by reference_type.
    reference_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reference_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # RESTRICT: the user named on an audit trail entry can't be deleted out
    # from under it.
    performed_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                  server_default=func.now())

    product: Mapped["Product"] = relationship()
    warehouse: Mapped["Warehouse"] = relationship()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (f"InventoryTransaction(type={self.transaction_type!r}, "
               f"quantity_change={self.quantity_change!r})")


class StockAdjustment(UUIDPKMixin, Base):
    """Records *why* a manual ADJUSTMENT ledger entry exists — the ledger
    row itself only carries the signed quantity, this carries the reason.
    One-to-one with the InventoryTransaction it produced.
    """
    __tablename__ = "stock_adjustments"
    __table_args__ = (
        CheckConstraint("quantity_change != 0", name="ck_stock_adjustments_quantity_nonzero"),
        CheckConstraint("length(trim(reason)) > 0", name="ck_stock_adjustments_reason_not_blank"),
        Index("ix_stock_adjustments_org_product_warehouse", "organization_id", "product_id",
             "warehouse_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False)
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id", ondelete="RESTRICT"), nullable=False)
    warehouse_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False)
    quantity_change: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False)
    reason: Mapped[str] = mapped_column(String(255), nullable=False)
    performed_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    # RESTRICT: this row exists to explain that ledger entry; the ledger
    # entry can't be deleted (nothing deletes InventoryTransaction rows at
    # all) so this FK never actually blocks anything, it just documents the
    # relationship for anyone reading the schema.
    inventory_transaction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("inventory_transactions.id", ondelete="RESTRICT"),
        nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                  server_default=func.now())

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"StockAdjustment(id={self.id!r}, reason={self.reason!r})"


class StockTransfer(UUIDPKMixin, Base):
    """Records a warehouse-to-warehouse transfer — the caller-facing
    operation that produces a TRANSFER_OUT + TRANSFER_IN ledger pair.
    """
    __tablename__ = "stock_transfers"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_stock_transfers_quantity_positive"),
        CheckConstraint("from_warehouse_id != to_warehouse_id",
                        name="ck_stock_transfers_distinct_warehouses"),
        Index("ix_stock_transfers_org_product", "organization_id", "product_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False)
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id", ondelete="RESTRICT"), nullable=False)
    from_warehouse_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False)
    to_warehouse_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    performed_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    out_transaction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("inventory_transactions.id", ondelete="RESTRICT"),
        nullable=False, unique=True)
    in_transaction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("inventory_transactions.id", ondelete="RESTRICT"),
        nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                  server_default=func.now())

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (f"StockTransfer(id={self.id!r}, from={self.from_warehouse_id!r}, "
               f"to={self.to_warehouse_id!r}, quantity={self.quantity!r})")
