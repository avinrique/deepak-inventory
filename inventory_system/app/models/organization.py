"""Organization — a tenant/company using the system, and the home for
every "Settings" value the app exposes (see app.ui.pages.settings_page):
company profile, inventory policy, sales/purchasing numbering and
defaults, security policy, and backup preferences. Grouped into sections
below purely for readability — they're all flat columns on one row (this
is a single-tenant-per-install desktop app in practice), which is also
what lets SqlOrganizationRepository.update stay a generic
setattr-from-dict loop instead of special-casing each section.
"""
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.backup import BackupFrequency
from app.domain.inventory import LowStockBehavior, StockValuationMethod
from app.models.base import Base, TimestampMixin, UUIDPKMixin


class Organization(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "organizations"
    __table_args__ = (
        CheckConstraint("length(trim(name)) > 0", name="ck_organizations_name_not_blank"),
        CheckConstraint("default_tax_percent >= 0 AND default_tax_percent <= 100",
                        name="ck_organizations_default_tax_percent_range"),
        CheckConstraint("default_discount_percent >= 0 AND default_discount_percent <= 100",
                        name="ck_organizations_default_discount_percent_range"),
        CheckConstraint("session_timeout_minutes > 0",
                        name="ck_organizations_session_timeout_positive"),
        CheckConstraint("password_min_length >= 4",
                        name="ck_organizations_password_min_length_floor"),
        CheckConstraint("backup_retention_count IS NULL OR backup_retention_count >= 0",
                        name="ck_organizations_backup_retention_non_negative"),
        Index("ix_organizations_name", "name", unique=True),
    )

    # -- Company --------------------------------------------------------#
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    legal_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Tax/registration id (e.g. PAN/VAT number) — free-text: format varies
    # by jurisdiction, validated in the service layer, not the database.
    tax_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    address: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Company contact details — shown on generated documents (invoices,
    # etc.) alongside name/legal_name/tax_id/address above. Kept optional:
    # a fresh organization has none of this filled in yet.
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    website: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Stored in the row rather than as a file path: keeps the logo inside
    # the same backup/restore boundary as everything else instead of
    # depending on a separate directory surviving alongside the database.
    logo_image: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    logo_content_type: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # -- Inventory -------------------------------------------------------#
    # Business rule, not a hard invariant: some organizations run a
    # sell-then-reconcile flow and need to let on-hand quantity go negative
    # temporarily. False by default — inventory operations reject anything
    # that would take a product's on-hand quantity below zero unless this
    # is explicitly turned on. See InventoryRepository._apply.
    allow_negative_stock: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # RESTRICT (not SET NULL): if this warehouse is the configured default,
    # deleting it must fail loudly rather than silently leaving Settings
    # pointing at nothing — the operator picks a new default first.
    #
    # organizations <-> warehouses is a genuine FK cycle (Warehouse also has
    # organization_id -> organizations.id): named + use_alter=True makes
    # SQLAlchemy emit this FK as a separate ALTER TABLE after both tables
    # exist (create_all) and DROP CONSTRAINT before DROP TABLE (drop_all —
    # notably what tests/conftest.py's live_db fixture does on teardown for
    # every DB-backed test), instead of an unresolvable CircularDependencyError.
    default_warehouse_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("warehouses.id", ondelete="RESTRICT", use_alter=True,
                  name="fk_organizations_default_warehouse_id_warehouses"),
        nullable=True)
    low_stock_behavior: Mapped[LowStockBehavior] = mapped_column(
        Enum(LowStockBehavior, name="low_stock_behavior", native_enum=True), nullable=False,
        default=LowStockBehavior.WARN_ONLY)
    stock_valuation_method: Mapped[StockValuationMethod] = mapped_column(
        Enum(StockValuationMethod, name="stock_valuation_method", native_enum=True),
        nullable=False, default=StockValuationMethod.WEIGHTED_AVERAGE)

    # -- Sales ------------------------------------------------------------#
    # The configurable half of invoice numbering (app.domain.sales.
    # format_invoice_number combines this with a locked per-organization
    # counter — see app.models.sales_order.InvoiceSequence — to produce
    # e.g. "INV-000042"). Free-text, not validated beyond non-blank in the
    # service layer: businesses use wildly different conventions.
    invoice_number_prefix: Mapped[str] = mapped_column(String(20), nullable=False,
                                                        default="INV-")
    default_tax_percent: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False,
                                                          default=0)
    default_discount_percent: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False,
                                                               default=0)

    # -- Purchasing --------------------------------------------------------#
    # Mirrors invoice_number_prefix exactly — see
    # app.models.purchase_order.PurchaseOrderSequence and
    # app.domain.purchasing.format_purchase_order_number.
    purchase_number_prefix: Mapped[str] = mapped_column(String(20), nullable=False,
                                                         default="PO-")

    # -- Security ----------------------------------------------------------#
    # Applied live to the running SessionManager on login and on save — see
    # SessionManager.set_idle_timeout and AuthService.login/
    # OrganizationService.update_organization.
    session_timeout_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    # See app.domain.security_policy — enforced on UserService.create_user's
    # initial_password and AuthService.change_password's new_password, not
    # on admin-generated temporary passwords (already high-entropy).
    password_min_length: Mapped[int] = mapped_column(Integer, nullable=False, default=8)
    password_require_uppercase: Mapped[bool] = mapped_column(Boolean, nullable=False,
                                                              default=False)
    password_require_number: Mapped[bool] = mapped_column(Boolean, nullable=False,
                                                           default=False)
    password_require_special_char: Mapped[bool] = mapped_column(Boolean, nullable=False,
                                                                 default=False)

    # -- Backup -------------------------------------------------------------#
    # Null means "use the installation's INVENTORY_BACKUP_DIR default" (see
    # app.config.settings) — this column lets an organization override that
    # per-install default without editing an env file. See
    # BackupService.create_backup.
    backup_directory: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    backup_frequency: Mapped[BackupFrequency] = mapped_column(
        Enum(BackupFrequency, name="backup_frequency", native_enum=True), nullable=False,
        default=BackupFrequency.MANUAL)
    # Null/0 means "keep every backup" — see BackupService's post-create
    # pruning step.
    backup_retention_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

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
