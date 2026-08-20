"""Supplier — organization-scoped, flat, same shape as Category/Brand/Unit.
No uniqueness constraint on name: two real suppliers can share a display
name (a franchise, a common surname) and nothing else here depends on it
being unique — unlike Product.sku or Warehouse.code, which are identifiers
looked up by exact value.
"""
import uuid

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPKMixin


class Supplier(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "suppliers"
    __table_args__ = (
        CheckConstraint("length(trim(name)) > 0", name="ck_suppliers_name_not_blank"),
        Index("ix_suppliers_org_name", "organization_id", "name"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    contact_person: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    address: Mapped[str | None] = mapped_column(String(500), nullable=True)
    tax_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Supplier(id={self.id!r}, name={self.name!r})"
