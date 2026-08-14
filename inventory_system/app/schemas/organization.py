import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel

from app.domain.backup import BackupFrequency
from app.domain.inventory import LowStockBehavior, StockValuationMethod


class OrganizationOut(BaseModel):
    id: uuid.UUID

    # -- Company ----------------------------------------------------------#
    name: str
    legal_name: str | None
    tax_id: str | None
    address: str | None
    phone: str | None
    email: str | None
    website: str | None
    is_active: bool
    logo_content_type: str | None
    has_logo: bool

    # -- Inventory ----------------------------------------------------------#
    allow_negative_stock: bool
    default_warehouse_id: uuid.UUID | None
    low_stock_behavior: LowStockBehavior
    stock_valuation_method: StockValuationMethod

    # -- Sales ----------------------------------------------------------------#
    invoice_number_prefix: str
    default_tax_percent: Decimal
    default_discount_percent: Decimal

    # -- Purchasing -------------------------------------------------------------#
    purchase_number_prefix: str

    # -- Security ---------------------------------------------------------------#
    session_timeout_minutes: int
    password_min_length: int
    password_require_uppercase: bool
    password_require_number: bool
    password_require_special_char: bool

    # -- Backup -------------------------------------------------------------------#
    backup_directory: str | None
    backup_frequency: BackupFrequency
    backup_retention_count: int | None

    created_at: datetime
    updated_at: datetime


class OrganizationUpdate(BaseModel):
    """All fields optional — a partial update only touches what's set."""
    # -- Company ----------------------------------------------------------#
    name: str | None = None
    legal_name: str | None = None
    tax_id: str | None = None
    address: str | None = None
    phone: str | None = None
    email: str | None = None
    website: str | None = None
    logo_image: bytes | None = None
    logo_content_type: str | None = None

    # -- Inventory ----------------------------------------------------------#
    allow_negative_stock: bool | None = None
    default_warehouse_id: uuid.UUID | None = None
    low_stock_behavior: LowStockBehavior | None = None
    stock_valuation_method: StockValuationMethod | None = None

    # -- Sales ----------------------------------------------------------------#
    invoice_number_prefix: str | None = None
    default_tax_percent: Decimal | None = None
    default_discount_percent: Decimal | None = None

    # -- Purchasing -------------------------------------------------------------#
    purchase_number_prefix: str | None = None

    # -- Security ---------------------------------------------------------------#
    session_timeout_minutes: int | None = None
    password_min_length: int | None = None
    password_require_uppercase: bool | None = None
    password_require_number: bool | None = None
    password_require_special_char: bool | None = None

    # -- Backup -------------------------------------------------------------------#
    backup_directory: str | None = None
    backup_frequency: BackupFrequency | None = None
    backup_retention_count: int | None = None
