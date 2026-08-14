"""Product brand — organization-scoped lookup, same shape as Category."""
import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPKMixin


class Brand(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "brands"
    __table_args__ = (
        CheckConstraint("length(trim(name)) > 0", name="ck_brands_name_not_blank"),
        Index("ix_brands_org_name", "organization_id", "name", unique=True),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Brand(id={self.id!r}, name={self.name!r})"
