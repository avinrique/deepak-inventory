"""Application service for Product CRUD, archive/restore, and search.

Validation/normalization lives in app.domain.product (pure, no I/O); this
orchestrates that plus ProductRepository — the one seam the UI is allowed
to call. organization_id always comes from the current session, never from
a caller-supplied argument, so a product operation can't be pointed at a
different tenant's catalog.
"""
import uuid
from datetime import datetime, timezone

from app.core.exceptions import (
    DuplicateBarcodeError,
    DuplicateSkuError,
    ProductNotFoundError,
    ProductValidationError,
)
from app.domain.product import ProductStatus, normalize_barcode, normalize_sku, validate_product
from app.repositories.interfaces import ProductRepository
from app.schemas.product import ProductCreate, ProductFilter, ProductOut, ProductPage, ProductUpdate
from app.security.authorization import require_permission
from app.security.session import SessionManager


class ProductService:
    def __init__(self, products: ProductRepository, sessions: SessionManager):
        self._products = products
        self._sessions = sessions

    def _organization_id(self) -> uuid.UUID:
        return self._sessions.current(now=datetime.now(timezone.utc)).organization_id

    @require_permission("product.create")
    def create_product(self, data: ProductCreate) -> ProductOut:
        org_id = self._organization_id()
        sku = normalize_sku(data.sku)
        barcode = normalize_barcode(data.barcode)

        errors = validate_product(sku=sku, name=data.name,
                                  purchase_price=data.purchase_price,
                                  selling_price=data.selling_price,
                                  tax_percent=data.tax_percent,
                                  minimum_stock_level=data.minimum_stock_level)
        if errors:
            raise ProductValidationError(errors)
        if self._products.sku_exists(org_id, sku):
            raise DuplicateSkuError(sku)
        if barcode and self._products.barcode_exists(org_id, barcode):
            raise DuplicateBarcodeError(barcode)

        normalized = data.model_copy(update={"sku": sku, "barcode": barcode})
        return self._products.create(org_id, normalized)

    @require_permission("product.update")
    def update_product(self, product_id: uuid.UUID, data: ProductUpdate) -> ProductOut:
        org_id = self._organization_id()
        existing = self._products.get_by_id(org_id, product_id)
        if existing is None:
            raise ProductNotFoundError(product_id)

        updates = data.model_dump(exclude_unset=True)
        sku = normalize_sku(updates["sku"]) if updates.get("sku") is not None else existing.sku
        barcode = (normalize_barcode(updates["barcode"]) if "barcode" in updates
                  else existing.barcode)
        name = updates.get("name", existing.name)
        purchase_price = updates.get("purchase_price", existing.purchase_price)
        selling_price = updates.get("selling_price", existing.selling_price)
        tax_percent = updates.get("tax_percent", existing.tax_percent)
        minimum_stock_level = updates.get("minimum_stock_level", existing.minimum_stock_level)

        errors = validate_product(sku=sku, name=name, purchase_price=purchase_price,
                                  selling_price=selling_price, tax_percent=tax_percent,
                                  minimum_stock_level=minimum_stock_level)
        if errors:
            raise ProductValidationError(errors)
        if sku != existing.sku and self._products.sku_exists(org_id, sku,
                                                              exclude_id=product_id):
            raise DuplicateSkuError(sku)
        if (barcode and barcode != existing.barcode
                and self._products.barcode_exists(org_id, barcode, exclude_id=product_id)):
            raise DuplicateBarcodeError(barcode)

        if "sku" in updates:
            updates["sku"] = sku
        if "barcode" in updates:
            updates["barcode"] = barcode
        result = self._products.update(org_id, product_id, ProductUpdate(**updates))
        if result is None:
            raise ProductNotFoundError(product_id)
        return result

    @require_permission("product.read")
    def get_product(self, product_id: uuid.UUID) -> ProductOut:
        result = self._products.get_by_id(self._organization_id(), product_id)
        if result is None:
            raise ProductNotFoundError(product_id)
        return result

    @require_permission("product.read")
    def search_products(self, filter: ProductFilter) -> ProductPage:
        return self._products.search(self._organization_id(), filter)

    @require_permission("product.delete")
    def archive_product(self, product_id: uuid.UUID) -> None:
        self._products.set_status(self._organization_id(), product_id, ProductStatus.ARCHIVED)

    @require_permission("product.update")
    def restore_product(self, product_id: uuid.UUID) -> None:
        self._products.set_status(self._organization_id(), product_id, ProductStatus.ACTIVE)

    @staticmethod
    def is_available_for_transactions(product: ProductOut) -> bool:
        """Archived products must not be used for new transactions — the
        one place that rule lives, so a future Sales/Purchases product
        picker calls this instead of re-deriving it (or forgetting it).
        """
        return product.status == ProductStatus.ACTIVE
