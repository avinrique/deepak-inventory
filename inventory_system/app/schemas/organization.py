import uuid
from datetime import datetime

from pydantic import BaseModel


class OrganizationOut(BaseModel):
    id: uuid.UUID
    name: str
    legal_name: str | None
    tax_id: str | None
    address: str | None
    phone: str | None
    email: str | None
    website: str | None
    is_active: bool
    allow_negative_stock: bool
    invoice_number_prefix: str
    created_at: datetime
    updated_at: datetime


class OrganizationUpdate(BaseModel):
    """All fields optional — a partial update only touches what's set."""
    name: str | None = None
    legal_name: str | None = None
    tax_id: str | None = None
    address: str | None = None
    phone: str | None = None
    email: str | None = None
    website: str | None = None
    allow_negative_stock: bool | None = None
    invoice_number_prefix: str | None = None
