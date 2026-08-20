"""Customer — organization-scoped, flat, same shape as Supplier plus the
fields Supplier doesn't need (customer_code, type, credit control). No
uniqueness constraint on name for the same reason as Supplier.name: two
real customers can share a display name and nothing here depends on exact-
match lookup by it. customer_code *is* unique per organization (when set)
— it's the human-facing identifier meant for exact lookup/reference,
same role Product.sku plays for products.

credit_limit=NULL means "no limit enforced" (distinct from 0, which would
mean "no credit allowed at all") — see SalesService's credit-control check.
opening_balance is the customer's balance as of when they were entered
into this system (e.g. migrating an existing customer with an existing
unpaid balance); it is added to the Invoice/Payment-derived balance, never
recomputed from transactions itself — see SqlCustomerRepository.get_balance.
"""
import uuid
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.sales import CustomerType
from app.models.base import Base, TimestampMixin, UUIDPKMixin


class Customer(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "customers"
    __table_args__ = (
        CheckConstraint("length(trim(name)) > 0", name="ck_customers_name_not_blank"),
        CheckConstraint("credit_limit IS NULL OR credit_limit >= 0",
                        name="ck_customers_credit_limit_non_negative"),
        CheckConstraint("opening_balance >= 0", name="ck_customers_opening_balance_non_negative"),
        Index("ix_customers_org_name", "organization_id", "name"),
        Index("ix_customers_org_code", "organization_id", "customer_code", unique=True),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False)
    customer_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    customer_type: Mapped[CustomerType] = mapped_column(
        Enum(CustomerType, name="customer_type", native_enum=True),
        nullable=False, default=CustomerType.INDIVIDUAL)
    contact_person: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    address: Mapped[str | None] = mapped_column(String(500), nullable=True)
    city: Mapped[str | None] = mapped_column(String(100), nullable=True)
    state: Mapped[str | None] = mapped_column(String(100), nullable=True)
    country: Mapped[str | None] = mapped_column(String(100), nullable=True)
    tax_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    credit_limit: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    opening_balance: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False,
                                                      default=Decimal("0"))
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Nullable only because existing rows predate this column (no creator
    # to backfill); every row created going forward always has one — see
    # SalesService.create_customer. RESTRICT, not SET NULL, once set: same
    # "don't silently lose who did this" policy as SalesOrder.created_by,
    # just optional here instead of required.
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Customer(id={self.id!r}, name={self.name!r})"
