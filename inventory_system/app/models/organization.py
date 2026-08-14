"""Organization — a tenant/company using the system. Inventory-related
entities (added later) will each carry an organization_id so data is
tenant-scoped from the start.
"""
import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPKMixin


class Organization(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "organizations"
    __table_args__ = (
        CheckConstraint("length(trim(name)) > 0", name="ck_organizations_name_not_blank"),
        Index("ix_organizations_name", "name", unique=True),
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    legal_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Tax/registration id (e.g. PAN) — free-text: format varies by
    # jurisdiction, validated in the service layer, not the database.
    tax_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    address: Mapped[str | None] = mapped_column(String(500), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Business rule, not a hard invariant: some organizations run a
    # sell-then-reconcile flow and need to let on-hand quantity go negative
    # temporarily. False by default — inventory operations reject anything
    # that would take a product's on-hand quantity below zero unless this
    # is explicitly turned on. See InventoryRepository.apply_transaction.
    allow_negative_stock: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # The configurable half of invoice numbering (app.domain.sales.
    # format_invoice_number combines this with a locked per-organization
    # counter — see app.models.sales_order.InvoiceSequence — to produce
    # e.g. "INV-000042"). Free-text, not validated beyond non-blank in the
    # service layer: businesses use wildly different conventions.
    invoice_number_prefix: Mapped[str] = mapped_column(String(20), nullable=False,
                                                        default="INV-")

    memberships: Mapped[list["UserOrganization"]] = relationship(
        back_populates="organization", cascade="all, delete-orphan")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Organization(id={self.id!r}, name={self.name!r})"


class UserOrganization(Base):
    """A user's membership in an organization, with the role they hold
    there. Composite PK (user_id, organization_id): a user belongs to a
    given organization at most once, with exactly one role at a time — the
    natural key already enforces that, so no surrogate id is needed.
    """
    __tablename__ = "user_organizations"
    __table_args__ = (
        Index("ix_user_organizations_organization_id", "organization_id"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"),
        primary_key=True)
    # RESTRICT, not CASCADE/SET NULL: a role that's actively assigned to a
    # member can't be deleted out from under them — the caller must
    # reassign the member to a different role first.
    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("roles.id", ondelete="RESTRICT"), nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now())

    user: Mapped["User"] = relationship(back_populates="memberships")
    organization: Mapped["Organization"] = relationship(back_populates="memberships")
    role: Mapped["Role"] = relationship()
