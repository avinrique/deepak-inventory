"""Purchasing workflow: PurchaseOrder + PurchaseOrderItem (the order),
GoodsReceipt + GoodsReceiptItem (a delivery event against that order,
possibly partial), PurchaseReturn (goods sent back to the supplier after
receipt).

Money/tax fields are Numeric, never float (see Decimal precedent in
app.domain.billing / app.models.product) — line totals are computed by
app.domain.purchasing, not stored, so there is nothing to keep in sync
when a DRAFT order's items change.

Receiving is where this ties into the inventory ledger:
GoodsReceiptItem.inventory_transaction_id and
PurchaseReturn.inventory_transaction_id each point at the
InventoryTransaction row that operation produced — see
app.repositories.sql.purchase_repository, which reuses
app.repositories.sql.inventory_repository's locked ``_apply`` helper
directly inside its own transaction so the inventory update, the PO status
change, and the audit log entry commit or roll back together.
"""
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, Date, DateTime, Enum, ForeignKey, Index, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.purchasing import PurchaseOrderStatus
from app.models.base import Base, TimestampMixin, UUIDPKMixin


class PurchaseOrder(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "purchase_orders"
    __table_args__ = (
        Index("ix_purchase_orders_org_status", "organization_id", "status"),
        Index("ix_purchase_orders_org_supplier", "organization_id", "supplier_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False)
    # RESTRICT: a supplier/warehouse referenced by a purchase order can't be
    # deleted out from under it — same policy as Product.category_id.
    supplier_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("suppliers.id", ondelete="RESTRICT"), nullable=False)
    warehouse_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False)
    status: Mapped[PurchaseOrderStatus] = mapped_column(
        Enum(PurchaseOrderStatus, name="purchase_order_status", native_enum=True),
        nullable=False, default=PurchaseOrderStatus.DRAFT)
    expected_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    items: Mapped[list["PurchaseOrderItem"]] = relationship(
        back_populates="purchase_order", cascade="all, delete-orphan",
        order_by="PurchaseOrderItem.created_at")
    supplier: Mapped["Supplier"] = relationship()
    warehouse: Mapped["Warehouse"] = relationship()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"PurchaseOrder(id={self.id!r}, status={self.status!r})"


class PurchaseOrderItem(UUIDPKMixin, TimestampMixin, Base):
    """Belongs fully to its PurchaseOrder (CASCADE) — items are only ever
    replaced wholesale while the order is DRAFT (see
    PurchaseService.update_purchase_order); once SUBMITTED they become
    immutable except for quantity_received, which
    InventoryRepository-adjacent code updates as goods are received/
    returned.
    """
    __tablename__ = "purchase_order_items"
    __table_args__ = (
        CheckConstraint("quantity_ordered > 0", name="ck_po_items_quantity_ordered_positive"),
        CheckConstraint("quantity_received >= 0", name="ck_po_items_quantity_received_non_negative"),
        CheckConstraint("quantity_received <= quantity_ordered",
                        name="ck_po_items_quantity_received_not_exceed_ordered"),
        CheckConstraint("unit_price >= 0", name="ck_po_items_unit_price_non_negative"),
        CheckConstraint("tax_percent >= 0 AND tax_percent <= 100",
                        name="ck_po_items_tax_percent_range"),
        Index("ix_po_items_purchase_order_id", "purchase_order_id"),
        Index("ix_po_items_product_id", "product_id"),
    )

    purchase_order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("purchase_orders.id", ondelete="CASCADE"),
        nullable=False)
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id", ondelete="RESTRICT"), nullable=False)
    quantity_ordered: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False)
    # Net quantity currently held from this order line: incremented by
    # GoodsReceiptItem, decremented by PurchaseReturn. Drives both the
    # "can't over-receive" check and the "can't return more than is
    # currently held" check off the same number.
    quantity_received: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False,
                                                        default=Decimal("0"))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    tax_percent: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False,
                                                 default=Decimal("0"))

    purchase_order: Mapped["PurchaseOrder"] = relationship(back_populates="items")
    product: Mapped["Product"] = relationship()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"PurchaseOrderItem(id={self.id!r}, product_id={self.product_id!r})"


class GoodsReceipt(UUIDPKMixin, Base):
    """One delivery event against a PurchaseOrder — may cover some or all
    of its line items, and may be one of several receipts against the same
    order (partial receiving). No TimestampMixin/updated_at: a receipt, like
    an InventoryTransaction, is never edited after it's recorded.
    """
    __tablename__ = "goods_receipts"
    __table_args__ = (
        Index("ix_goods_receipts_org_purchase_order", "organization_id", "purchase_order_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False)
    purchase_order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("purchase_orders.id", ondelete="RESTRICT"),
        nullable=False)
    warehouse_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False)
    received_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                   server_default=func.now())

    items: Mapped[list["GoodsReceiptItem"]] = relationship(
        back_populates="goods_receipt", cascade="all, delete-orphan")
    purchase_order: Mapped["PurchaseOrder"] = relationship()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"GoodsReceipt(id={self.id!r}, purchase_order_id={self.purchase_order_id!r})"


class GoodsReceiptItem(UUIDPKMixin, Base):
    __tablename__ = "goods_receipt_items"
    __table_args__ = (
        CheckConstraint("quantity_received > 0",
                        name="ck_goods_receipt_items_quantity_positive"),
        Index("ix_goods_receipt_items_goods_receipt_id", "goods_receipt_id"),
    )

    goods_receipt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("goods_receipts.id", ondelete="CASCADE"),
        nullable=False)
    # RESTRICT: a PurchaseOrderItem with receiving history against it can't
    # be deleted (moot in practice — items are immutable once SUBMITTED,
    # well before any receipt can exist, but stated explicitly).
    purchase_order_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("purchase_order_items.id", ondelete="RESTRICT"),
        nullable=False)
    quantity_received: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False)
    inventory_transaction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("inventory_transactions.id", ondelete="RESTRICT"),
        nullable=False, unique=True)

    goods_receipt: Mapped["GoodsReceipt"] = relationship(back_populates="items")
    purchase_order_item: Mapped["PurchaseOrderItem"] = relationship()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"GoodsReceiptItem(id={self.id!r}, quantity_received={self.quantity_received!r})"


class PurchaseReturn(UUIDPKMixin, Base):
    __tablename__ = "purchase_returns"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_purchase_returns_quantity_positive"),
        CheckConstraint("length(trim(reason)) > 0", name="ck_purchase_returns_reason_not_blank"),
        Index("ix_purchase_returns_org_purchase_order", "organization_id", "purchase_order_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False)
    purchase_order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("purchase_orders.id", ondelete="RESTRICT"),
        nullable=False)
    purchase_order_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("purchase_order_items.id", ondelete="RESTRICT"),
        nullable=False)
    warehouse_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False)
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id", ondelete="RESTRICT"), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False)
    reason: Mapped[str] = mapped_column(String(255), nullable=False)
    returned_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    inventory_transaction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("inventory_transactions.id", ondelete="RESTRICT"),
        nullable=False, unique=True)
    returned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                   server_default=func.now())

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"PurchaseReturn(id={self.id!r}, quantity={self.quantity!r})"
