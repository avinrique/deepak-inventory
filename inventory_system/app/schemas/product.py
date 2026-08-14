import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel

from app.domain.product import ProductStatus


class CategoryOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None


class CategoryCreate(BaseModel):
    name: str
    description: str | None = None


class BrandOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None


class BrandCreate(BaseModel):
    name: str
    description: str | None = None


class UnitOut(BaseModel):
    id: uuid.UUID
    name: str
    abbreviation: str


class UnitCreate(BaseModel):
    name: str
    abbreviation: str


class ProductCreate(BaseModel):
    sku: str
    barcode: str | None = None
    name: str
    description: str | None = None
    category_id: uuid.UUID | None = None
    brand_id: uuid.UUID | None = None
    unit_id: uuid.UUID
    purchase_price: Decimal = Decimal("0")
    selling_price: Decimal = Decimal("0")
    tax_percent: Decimal = Decimal("0")
    minimum_stock_level: Decimal = Decimal("0")


class ProductUpdate(BaseModel):
    """All fields optional — a partial update only touches what's set."""
    sku: str | None = None
    barcode: str | None = None
    name: str | None = None
    description: str | None = None
    category_id: uuid.UUID | None = None
    brand_id: uuid.UUID | None = None
    unit_id: uuid.UUID | None = None
    purchase_price: Decimal | None = None
    selling_price: Decimal | None = None
    tax_percent: Decimal | None = None
    minimum_stock_level: Decimal | None = None


class ProductOut(BaseModel):
    id: uuid.UUID
    sku: str
    barcode: str | None
    name: str
    description: str | None
    category: CategoryOut | None
    brand: BrandOut | None
    unit: UnitOut
    purchase_price: Decimal
    selling_price: Decimal
    tax_percent: Decimal
    minimum_stock_level: Decimal
    status: ProductStatus
    created_at: datetime
    updated_at: datetime


class ProductFilter(BaseModel):
    search: str | None = None            # matches sku, barcode, or name
    category_id: uuid.UUID | None = None
    brand_id: uuid.UUID | None = None
    status: ProductStatus | None = None
    sort_by: str = "name"                # one of: name, sku, purchase_price,
                                          # selling_price, created_at
    sort_desc: bool = False
    page: int = 1
    page_size: int = 25


class ProductPage(BaseModel):
    items: list[ProductOut]
    total: int
    page: int
    page_size: int

    @property
    def total_pages(self) -> int:
        if self.page_size <= 0:
            return 1
        return max(1, -(-self.total // self.page_size))  # ceil division
