"""Unit of measure (e.g. "Kilogram" / "kg") — organization-scoped. Required
on every Product (unlike Category/Brand, which are optional): a quantity
without a unit isn't meaningful.
"""
import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPKMixin


class Unit(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "units"
    __table_args__ = (
        CheckConstraint("length(trim(name)) > 0", name="ck_units_name_not_blank"),
        CheckConstraint("length(trim(abbreviation)) > 0",
                        name="ck_units_abbreviation_not_blank"),
        Index("ix_units_org_name", "organization_id", "name", unique=True),
        Index("ix_units_org_abbreviation", "organization_id", "abbreviation", unique=True),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    abbreviation: Mapped[str] = mapped_column(String(20), nullable=False)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Unit(id={self.id!r}, abbreviation={self.abbreviation!r})"
