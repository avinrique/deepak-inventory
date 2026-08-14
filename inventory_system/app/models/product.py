"""Product — the catalog entity. organization_id-scoped like every other
inventory entity in this schema.

Field-level decisions:
- unit_id is required (a quantity without a unit isn't meaningful);
  category_id/brand_id are optional (many small catalogs don't categorize
  everything on day one).
- sku is stored uppercase-trimmed (CHECK enforces it — see
  app.domain.product.normalize_sku, the single place that normalizes it on
  the write path) and unique per organization.
- barcode is unique per organization *only when present* — a partial
  unique index (``postgresql_where``), not a plain unique index, which
  would otherwise reject a second NULL barcode (SQL NULLs are distinct
  from each other in a normal unique index, but being explicit here avoids
  relying on that behavior).
- status is an archive flag, not a hard delete: "archived" products stay
  in the database (referenced by historical sales/purchases) but are
  excluded from new-transaction pickers by the service layer.
- Money/tax/quantity are Numeric, never float — see Decimal precedent set
  in app.domain.billing.
"""
import uuid
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.product import ProductStatus
from app.models.base import Base, TimestampMixin, UUIDPKMixin


class Product(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "products"
    __table_args__ = (
        CheckConstraint("sku = upper(sku)", name="ck_products_sku_uppercase"),
        CheckConstraint("length(trim(sku)) > 0", name="ck_products_sku_not_blank"),
        CheckConstraint("length(trim(name)) > 0", name="ck_products_name_not_blank"),
        CheckConstraint("purchase_price >= 0", name="ck_products_purchase_price_non_negative"),
        CheckConstraint("selling_price >= 0", name="ck_products_selling_price_non_negative"),
        CheckConstraint("tax_percent >= 0 AND tax_percent <= 100",
                        name="ck_products_tax_percent_range"),
        CheckConstraint("minimum_stock_level >= 0",
                        name="ck_products_minimum_stock_level_non_negative"),
        Index("ix_products_org_sku", "organization_id", "sku", unique=True),
        Index("ix_products_org_barcode", "organization_id", "barcode", unique=True,
             postgresql_where="barcode IS NOT NULL"),
        Index("ix_products_org_status", "organization_id", "status"),
        Index("ix_products_org_name", "organization_id", "name"),
        Index("ix_products_category_id", "category_id"),
        Index("ix_products_brand_id", "brand_id"),
        Index("ix_products_unit_id", "unit_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False)
    sku: Mapped[str] = mapped_column(String(64), nullable=False)
    barcode: Mapped[str | None] = mapped_column(String(64), nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # RESTRICT: a category/brand/unit in use by a product can't be deleted
    # out from under it — the caller reassigns products first, same policy
    # as UserOrganization.role_id.
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("categories.id", ondelete="RESTRICT"), nullable=True)
    brand_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("brands.id", ondelete="RESTRICT"), nullable=True)
    unit_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("units.id", ondelete="RESTRICT"), nullable=False)

    purchase_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False,
                                                     default=Decimal("0"))
    selling_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False,
                                                    default=Decimal("0"))
    tax_percent: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False,
                                                 default=Decimal("0"))
    minimum_stock_level: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False,
                                                         default=Decimal("0"))
    status: Mapped[ProductStatus] = mapped_column(
        Enum(ProductStatus, name="product_status", native_enum=True), nullable=False,
        default=ProductStatus.ACTIVE)

    category: Mapped["Category | None"] = relationship()
    brand: Mapped["Brand | None"] = relationship()
    unit: Mapped["Unit"] = relationship()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Product(id={self.id!r}, sku={self.sku!r})"
