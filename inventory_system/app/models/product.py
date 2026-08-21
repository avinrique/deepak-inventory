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
- product_type controls which Add Product fields are relevant (GOODS are
  inventory-tracked; SERVICE/EXPENSE are not — see
  app.domain.product.validate_opening_stock) and defaults to GOODS so
  every existing product (created before this column existed) keeps its
  current, correct behavior.
- sub_unit_id/sub_unit_conversion_factor (and the tertiary pair) are real,
  functional columns — sub_unit_conversion_factor is how many base
  unit_id units equal one sub-unit, a Decimal usable in an actual
  quantity calculation, not a display-only string. Both nullable: most
  products only need one unit. RESTRICT like unit_id, for the same
  reason. CHECK constraints enforce "both or neither" and "conversion
  factor is a positive multiplier" at the database level too, not just in
  app.domain.product.validate_product.
- is_taxable is a real, explicit flag rather than overloading
  tax_percent == 0 — a CHECK constraint enforces they can't disagree
  (non-taxable must carry tax_percent 0), so "why is this 0%" is never
  ambiguous between "no tax applies" and "someone typed 0."
- hsn_code/dftqc_no/country_of_origin/size/color/flavour/expiry_date are
  optional free-text/date attributes with no cross-field business logic
  of their own (unlike the unit/tax fields above) — plain nullable
  columns, normalized blank-to-NULL the same way barcode already is (see
  app.domain.product.normalize_optional_text).
"""
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.product import ProductStatus, ProductType
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
        CheckConstraint("excise_percent >= 0 AND excise_percent <= 100",
                        name="ck_products_excise_percent_range"),
        CheckConstraint("minimum_stock_level >= 0",
                        name="ck_products_minimum_stock_level_non_negative"),
        CheckConstraint("is_taxable OR tax_percent = 0",
                        name="ck_products_non_taxable_has_zero_tax"),
        CheckConstraint(
            "(sub_unit_id IS NULL) = (sub_unit_conversion_factor IS NULL)",
            name="ck_products_sub_unit_conversion_paired"),
        CheckConstraint("sub_unit_conversion_factor IS NULL OR sub_unit_conversion_factor > 0",
                        name="ck_products_sub_unit_conversion_positive"),
        CheckConstraint("sub_unit_id IS NULL OR sub_unit_id != unit_id",
                        name="ck_products_sub_unit_differs_from_unit"),
        CheckConstraint(
            "(tertiary_unit_id IS NULL) = (tertiary_unit_conversion_factor IS NULL)",
            name="ck_products_tertiary_unit_conversion_paired"),
        CheckConstraint(
            "tertiary_unit_conversion_factor IS NULL OR tertiary_unit_conversion_factor > 0",
            name="ck_products_tertiary_unit_conversion_positive"),
        CheckConstraint("tertiary_unit_id IS NULL OR tertiary_unit_id != unit_id",
                        name="ck_products_tertiary_unit_differs_from_unit"),
        CheckConstraint(
            "tertiary_unit_id IS NULL OR sub_unit_id IS NULL "
            "OR tertiary_unit_id != sub_unit_id",
            name="ck_products_tertiary_unit_differs_from_sub_unit"),
        Index("ix_products_org_sku", "organization_id", "sku", unique=True),
        Index("ix_products_org_barcode", "organization_id", "barcode", unique=True,
             postgresql_where="barcode IS NOT NULL"),
        Index("ix_products_org_status", "organization_id", "status"),
        Index("ix_products_org_name", "organization_id", "name"),
        Index("ix_products_category_id", "category_id"),
        Index("ix_products_brand_id", "brand_id"),
        Index("ix_products_unit_id", "unit_id"),
        Index("ix_products_org_type", "organization_id", "product_type"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False)
    sku: Mapped[str] = mapped_column(String(64), nullable=False)
    barcode: Mapped[str | None] = mapped_column(String(64), nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    product_type: Mapped[ProductType] = mapped_column(
        Enum(ProductType, name="product_type", native_enum=True), nullable=False,
        default=ProductType.GOODS)

    # RESTRICT: a category/brand/unit in use by a product can't be deleted
    # out from under it — the caller reassigns products first, same policy
    # as UserOrganization.role_id.
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("categories.id", ondelete="RESTRICT"), nullable=True)
    brand_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("brands.id", ondelete="RESTRICT"), nullable=True)
    unit_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("units.id", ondelete="RESTRICT"), nullable=False)
    sub_unit_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("units.id", ondelete="RESTRICT"), nullable=True)
    sub_unit_conversion_factor: Mapped[Decimal | None] = mapped_column(
        Numeric(14, 6), nullable=True)
    tertiary_unit_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("units.id", ondelete="RESTRICT"), nullable=True)
    tertiary_unit_conversion_factor: Mapped[Decimal | None] = mapped_column(
        Numeric(14, 6), nullable=True)

    purchase_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False,
                                                     default=Decimal("0"))
    selling_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False,
                                                    default=Decimal("0"))
    tax_percent: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False,
                                                 default=Decimal("0"))
    is_taxable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Independent of is_taxable/tax_percent — a government levy distinct
    # from sales tax, so a non-taxable product can still carry excise duty.
    excise_percent: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False,
                                                     default=Decimal("0"))
    minimum_stock_level: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False,
                                                         default=Decimal("0"))
    status: Mapped[ProductStatus] = mapped_column(
        Enum(ProductStatus, name="product_status", native_enum=True), nullable=False,
        default=ProductStatus.ACTIVE)

    hsn_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    size: Mapped[str | None] = mapped_column(String(64), nullable=True)
    color: Mapped[str | None] = mapped_column(String(64), nullable=True)
    flavour: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dftqc_no: Mapped[str | None] = mapped_column(String(64), nullable=True)
    country_of_origin: Mapped[str | None] = mapped_column(String(100), nullable=True)
    expiry_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    category: Mapped["Category | None"] = relationship()
    brand: Mapped["Brand | None"] = relationship()
    unit: Mapped["Unit"] = relationship(foreign_keys=[unit_id])
    sub_unit: Mapped["Unit | None"] = relationship(foreign_keys=[sub_unit_id])
    tertiary_unit: Mapped["Unit | None"] = relationship(foreign_keys=[tertiary_unit_id])

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Product(id={self.id!r}, sku={self.sku!r})"
