"""Sales workflow: SalesOrder + SalesOrderItem (the order), Invoice (a
frozen financial snapshot generated once the order is FULFILLED), Payment
(money received against an Invoice), SalesReturn (goods sent back by the
customer after fulfillment), and InvoiceSequence (the per-organization
counter backing configurable, unique invoice numbering).

Money/tax fields are Numeric, never float (see Decimal precedent in
app.domain.pricing / app.models.product). Invoice.subtotal/tax_amount/
total_amount are a deliberate exception to "compute from items, don't
store it" (contrast PurchaseOrderOut's computed properties): an invoice is
a financial document that must stay fixed at the values it was generated
with even if something upstream could theoretically change afterwards —
so those three are snapshotted once, at generation time, in
app.repositories.sql.sales_repository.

Fulfillment ties into the inventory ledger the same way Purchasing's
goods-receipt does: SalesOrderRepository.fulfill_sale and .record_return
reuse app.repositories.sql.inventory_repository's locked ``_apply`` helper
directly inside their own transaction, so the inventory update, the order
status change, and the audit log entry commit or roll back together.
"""
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.sales import PaymentMethod, SalesOrderStatus
from app.models.base import Base, TimestampMixin, UUIDPKMixin


class SalesOrder(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "sales_orders"
    __table_args__ = (
        Index("ix_sales_orders_org_status", "organization_id", "status"),
        Index("ix_sales_orders_org_customer", "organization_id", "customer_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False)
    # RESTRICT: a customer/warehouse referenced by a sales order can't be
    # deleted out from under it — same policy as PurchaseOrder.supplier_id.
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False)
    warehouse_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False)
    status: Mapped[SalesOrderStatus] = mapped_column(
        Enum(SalesOrderStatus, name="sales_order_status", native_enum=True),
        nullable=False, default=SalesOrderStatus.DRAFT)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    confirmed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),
                                                          nullable=True)

    items: Mapped[list["SalesOrderItem"]] = relationship(
        back_populates="sales_order", cascade="all, delete-orphan",
        order_by="SalesOrderItem.created_at")
    customer: Mapped["Customer"] = relationship()
    warehouse: Mapped["Warehouse"] = relationship()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"SalesOrder(id={self.id!r}, status={self.status!r})"


class SalesOrderItem(UUIDPKMixin, TimestampMixin, Base):
    """Belongs fully to its SalesOrder (CASCADE) — items are only ever
    replaced wholesale while the order is DRAFT (see
    SalesService.update_sales_order); once CONFIRMED they become immutable
    except for quantity_fulfilled, which fulfillment/return update.
    """
    __tablename__ = "sales_order_items"
    __table_args__ = (
        CheckConstraint("quantity_ordered > 0", name="ck_so_items_quantity_ordered_positive"),
        CheckConstraint("quantity_fulfilled >= 0",
                        name="ck_so_items_quantity_fulfilled_non_negative"),
        CheckConstraint("quantity_fulfilled <= quantity_ordered",
                        name="ck_so_items_quantity_fulfilled_not_exceed_ordered"),
        CheckConstraint("unit_price >= 0", name="ck_so_items_unit_price_non_negative"),
        CheckConstraint("tax_percent >= 0 AND tax_percent <= 100",
                        name="ck_so_items_tax_percent_range"),
        CheckConstraint("discount_percent >= 0 AND discount_percent <= 100",
                        name="ck_so_items_discount_percent_range"),
        Index("ix_so_items_sales_order_id", "sales_order_id"),
        Index("ix_so_items_product_id", "product_id"),
    )

    sales_order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sales_orders.id", ondelete="CASCADE"), nullable=False)
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id", ondelete="RESTRICT"), nullable=False)
    quantity_ordered: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False)
    # Net quantity currently out with the customer from this order line:
    # set to quantity_ordered by fulfill_sale (fulfillment is all-or-
    # nothing — there is no partial-fulfillment feature here, unlike
    # Purchasing's partial receiving), decremented by SalesReturn.
    quantity_fulfilled: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False,
                                                        default=Decimal("0"))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    tax_percent: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False,
                                                 default=Decimal("0"))
    # Applied to this line's subtotal *before* tax (tax is charged on the
    # discounted price) — see app.domain.sales.line_subtotal_after_discount.
    # Purchasing has no equivalent field: a supplier's price is what it is.
    discount_percent: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False,
                                                       default=Decimal("0"))

    sales_order: Mapped["SalesOrder"] = relationship(back_populates="items")
    product: Mapped["Product"] = relationship()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"SalesOrderItem(id={self.id!r}, product_id={self.product_id!r})"


class Invoice(UUIDPKMixin, Base):
    """At most one per SalesOrder (unique sales_order_id) — generated once
    the order is FULFILLED. No TimestampMixin/updated_at: an invoice, like
    an InventoryTransaction, is never edited after it's generated.
    """
    __tablename__ = "invoices"
    __table_args__ = (
        CheckConstraint("subtotal >= 0", name="ck_invoices_subtotal_non_negative"),
        CheckConstraint("tax_amount >= 0", name="ck_invoices_tax_amount_non_negative"),
        CheckConstraint("total_amount >= 0", name="ck_invoices_total_amount_non_negative"),
        Index("ix_invoices_org_invoice_number", "organization_id", "invoice_number",
             unique=True),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False)
    sales_order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sales_orders.id", ondelete="RESTRICT"),
        nullable=False, unique=True)
    invoice_number: Mapped[str] = mapped_column(String(64), nullable=False)
    subtotal: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    tax_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    generated_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                    server_default=func.now())

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Invoice(id={self.id!r}, invoice_number={self.invoice_number!r})"


class Payment(UUIDPKMixin, Base):
    """Immutable once recorded — no void/edit feature. Completion of the
    order (FULFILLED -> COMPLETED) is derived from the running sum of
    Payment.amount against an Invoice reaching its total_amount; see
    app.repositories.sql.sales_repository.record_payment.
    """
    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_payments_amount_positive"),
        Index("ix_payments_org_invoice_id", "organization_id", "invoice_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False)
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoices.id", ondelete="RESTRICT"), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    method: Mapped[PaymentMethod] = mapped_column(
        Enum(PaymentMethod, name="payment_method", native_enum=True), nullable=False)
    recorded_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                   server_default=func.now())

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Payment(id={self.id!r}, amount={self.amount!r})"


class SalesReturn(UUIDPKMixin, Base):
    __tablename__ = "sales_returns"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_sales_returns_quantity_positive"),
        CheckConstraint("length(trim(reason)) > 0", name="ck_sales_returns_reason_not_blank"),
        Index("ix_sales_returns_org_sales_order", "organization_id", "sales_order_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False)
    sales_order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sales_orders.id", ondelete="RESTRICT"), nullable=False)
    sales_order_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sales_order_items.id", ondelete="RESTRICT"),
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
        return f"SalesReturn(id={self.id!r}, quantity={self.quantity!r})"


class InvoiceSequence(Base):
    """Backs configurable, unique invoice numbering: one row per
    organization, incremented under a row lock inside the same transaction
    as the Invoice it's numbering (see
    SalesOrderRepository.generate_invoice) — not a Postgres SEQUENCE,
    because a native sequence's counter does not roll back if the
    surrounding transaction aborts, which would burn numbers on failed
    invoice attempts; an ordinary locked table row rolls back correctly
    along with everything else.
    """
    __tablename__ = "invoice_sequences"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"),
        primary_key=True)
    next_value: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"InvoiceSequence(organization_id={self.organization_id!r}, next_value={self.next_value!r})"
