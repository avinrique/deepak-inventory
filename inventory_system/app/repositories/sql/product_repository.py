"""SQLAlchemy-backed ProductRepository — real implementation, no excel/
equivalent (products are a new capability the legacy app never had).

SKU/barcode normalization and validation happen in app.domain.product /
ProductService, not here — this repository only persists and queries what
it's given, and translates ORM rows to schemas.
"""
import uuid

from sqlalchemy import or_

from app.database.session import get_session
from app.domain.product import ProductStatus
from app.models import Product
from app.schemas.product import (
    BrandOut,
    CategoryOut,
    ProductCreate,
    ProductFilter,
    ProductOut,
    ProductPage,
    ProductUpdate,
    UnitOut,
)

_SORT_COLUMNS = {
    "name": Product.name,
    "sku": Product.sku,
    "purchase_price": Product.purchase_price,
    "selling_price": Product.selling_price,
    "created_at": Product.created_at,
}


def _to_out(product: Product) -> ProductOut:
    return ProductOut(
        id=product.id, sku=product.sku, barcode=product.barcode, name=product.name,
        description=product.description,
        category=(CategoryOut(id=product.category.id, name=product.category.name,
                              description=product.category.description)
                 if product.category else None),
        brand=(BrandOut(id=product.brand.id, name=product.brand.name,
                       description=product.brand.description)
              if product.brand else None),
        unit=UnitOut(id=product.unit.id, name=product.unit.name,
                    abbreviation=product.unit.abbreviation),
        purchase_price=product.purchase_price, selling_price=product.selling_price,
        tax_percent=product.tax_percent, minimum_stock_level=product.minimum_stock_level,
        status=product.status, created_at=product.created_at, updated_at=product.updated_at)


class SqlProductRepository:
    def create(self, organization_id: uuid.UUID, data: ProductCreate) -> ProductOut:
        with get_session() as db:
            product = Product(organization_id=organization_id, sku=data.sku,
                             barcode=data.barcode, name=data.name,
                             description=data.description, category_id=data.category_id,
                             brand_id=data.brand_id, unit_id=data.unit_id,
                             purchase_price=data.purchase_price,
                             selling_price=data.selling_price, tax_percent=data.tax_percent,
                             minimum_stock_level=data.minimum_stock_level)
            db.add(product)
            db.flush()
            return _to_out(product)

    def update(self, organization_id: uuid.UUID, product_id: uuid.UUID,
              data: ProductUpdate) -> ProductOut | None:
        with get_session() as db:
            product = db.get(Product, product_id)
            if product is None or product.organization_id != organization_id:
                return None
            for field, value in data.model_dump(exclude_unset=True).items():
                setattr(product, field, value)
            db.flush()
            return _to_out(product)

    def get_by_id(self, organization_id: uuid.UUID,
                 product_id: uuid.UUID) -> ProductOut | None:
        with get_session() as db:
            product = db.get(Product, product_id)
            if product is None or product.organization_id != organization_id:
                return None
            return _to_out(product)

    def sku_exists(self, organization_id: uuid.UUID, sku: str,
                   exclude_id: uuid.UUID | None = None) -> bool:
        with get_session() as db:
            query = db.query(Product.id).filter_by(organization_id=organization_id, sku=sku)
            if exclude_id is not None:
                query = query.filter(Product.id != exclude_id)
            return query.first() is not None

    def barcode_exists(self, organization_id: uuid.UUID, barcode: str,
                       exclude_id: uuid.UUID | None = None) -> bool:
        with get_session() as db:
            query = db.query(Product.id).filter_by(organization_id=organization_id,
                                                    barcode=barcode)
            if exclude_id is not None:
                query = query.filter(Product.id != exclude_id)
            return query.first() is not None

    def set_status(self, organization_id: uuid.UUID, product_id: uuid.UUID,
                   status: ProductStatus) -> None:
        with get_session() as db:
            product = db.get(Product, product_id)
            if product is not None and product.organization_id == organization_id:
                product.status = status

    def search(self, organization_id: uuid.UUID, filter: ProductFilter) -> ProductPage:
        with get_session() as db:
            query = db.query(Product).filter(Product.organization_id == organization_id)
            if filter.search:
                pattern = f"%{filter.search.strip()}%"
                query = query.filter(or_(Product.name.ilike(pattern),
                                         Product.sku.ilike(pattern),
                                         Product.barcode.ilike(pattern)))
            if filter.category_id:
                query = query.filter(Product.category_id == filter.category_id)
            if filter.brand_id:
                query = query.filter(Product.brand_id == filter.brand_id)
            if filter.status:
                query = query.filter(Product.status == filter.status)

            total = query.count()

            sort_column = _SORT_COLUMNS.get(filter.sort_by, Product.name)
            query = query.order_by(sort_column.desc() if filter.sort_desc
                                   else sort_column.asc())

            page = max(1, filter.page)
            page_size = max(1, filter.page_size)
            rows = query.offset((page - 1) * page_size).limit(page_size).all()

            return ProductPage(items=[_to_out(p) for p in rows], total=total, page=page,
                               page_size=page_size)
