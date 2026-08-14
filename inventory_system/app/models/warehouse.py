"""Warehouse — a physical/logical stock location. Organization-scoped, flat
(no nesting), same shape as Category/Brand/Unit. Every Inventory row and
InventoryTransaction is scoped to one of these — the system supports
multiple warehouses per organization from the start, not bolted on later.
"""
import uuid

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPKMixin


class Warehouse(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "warehouses"
    __table_args__ = (
        CheckConstraint("code = upper(code)", name="ck_warehouses_code_uppercase"),
        CheckConstraint("length(trim(code)) > 0", name="ck_warehouses_code_not_blank"),
        CheckConstraint("length(trim(name)) > 0", name="ck_warehouses_name_not_blank"),
        Index("ix_warehouses_org_code", "organization_id", "code", unique=True),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False)
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    address: Mapped[str | None] = mapped_column(String(500), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Warehouse(id={self.id!r}, code={self.code!r})"
