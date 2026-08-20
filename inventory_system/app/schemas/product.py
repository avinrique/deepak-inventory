import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel

from app.domain.product import ProductStatus, ProductType


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
    product_type: ProductType = ProductType.GOODS
    category_id: uuid.UUID | None = None
    brand_id: uuid.UUID | None = None
    unit_id: uuid.UUID
    sub_unit_id: uuid.UUID | None = None
    sub_unit_conversion_factor: Decimal | None = None
    tertiary_unit_id: uuid.UUID | None = None
    tertiary_unit_conversion_factor: Decimal | None = None
    purchase_price: Decimal = Decimal("0")
    selling_price: Decimal = Decimal("0")
    tax_percent: Decimal = Decimal("0")
    is_taxable: bool = True
    minimum_stock_level: Decimal = Decimal("0")
    hsn_code: str | None = None
    size: str | None = None
    color: str | None = None
    flavour: str | None = None
    dftqc_no: str | None = None
    country_of_origin: str | None = None
    expiry_date: date | None = None


class ProductUpdate(BaseModel):
    """All fields optional — a partial update only touches what's set."""
    sku: str | None = None
    barcode: str | None = None
    name: str | None = None
    description: str | None = None
    product_type: ProductType | None = None
    category_id: uuid.UUID | None = None
    brand_id: uuid.UUID | None = None
    unit_id: uuid.UUID | None = None
    sub_unit_id: uuid.UUID | None = None
    sub_unit_conversion_factor: Decimal | None = None
    tertiary_unit_id: uuid.UUID | None = None
    tertiary_unit_conversion_factor: Decimal | None = None
    purchase_price: Decimal | None = None
    selling_price: Decimal | None = None
    tax_percent: Decimal | None = None
    is_taxable: bool | None = None
    minimum_stock_level: Decimal | None = None
    hsn_code: str | None = None
    size: str | None = None
    color: str | None = None
    flavour: str | None = None
    dftqc_no: str | None = None
    country_of_origin: str | None = None
    expiry_date: date | None = None


class ProductOut(BaseModel):
    id: uuid.UUID
    sku: str
    barcode: str | None
    name: str
    description: str | None
    product_type: ProductType
    category: CategoryOut | None
    brand: BrandOut | None
    unit: UnitOut
    sub_unit: UnitOut | None
    sub_unit_conversion_factor: Decimal | None
    tertiary_unit: UnitOut | None
    tertiary_unit_conversion_factor: Decimal | None
    purchase_price: Decimal
    selling_price: Decimal
    tax_percent: Decimal
    is_taxable: bool
    minimum_stock_level: Decimal
    hsn_code: str | None
    size: str | None
    color: str | None
    flavour: str | None
    dftqc_no: str | None
    country_of_origin: str | None
    expiry_date: date | None
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
