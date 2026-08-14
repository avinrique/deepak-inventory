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
from app.repositories.interfaces import AuditLogRepository, ProductRepository
from app.schemas.product import ProductCreate, ProductFilter, ProductOut, ProductPage, ProductUpdate
from app.security.authorization import require_permission
from app.security.session import SessionManager


class ProductService:
    def __init__(self, products: ProductRepository, sessions: SessionManager,
                audit_log: AuditLogRepository):
        self._products = products
        self._sessions = sessions
        self._audit_log = audit_log

    def _organization_id(self) -> uuid.UUID:
        return self._sessions.current(now=datetime.now(timezone.utc)).organization_id

    def _audit(self, *, action: str, entity_id: uuid.UUID, changes: dict | None = None) -> None:
        session = self._sessions.peek()
        actor_id = session.user_id if session else None
        self._audit_log.record(organization_id=self._organization_id(), user_id=actor_id,
                               actor_email=None, organization_name=None, action=action,
                               entity_type="product", entity_id=entity_id, changes=changes)

    @require_permission("products.create")
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
        created = self._products.create(org_id, normalized)
        self._audit(action="product.create", entity_id=created.id,
                   changes={"sku": created.sku, "name": created.name,
                           "purchase_price": str(created.purchase_price),
                           "selling_price": str(created.selling_price)})
        return created

    @require_permission("products.update")
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

        # Only the fields the caller actually set, and only where the value
        # genuinely changed — a before/after diff, not a full-record dump.
        before, after = {}, {}
        for field in updates:
            old_value, new_value = getattr(existing, field), getattr(result, field)
            if old_value != new_value:
                before[field] = str(old_value)
                after[field] = str(new_value)
        if before:
            self._audit(action="product.update", entity_id=product_id,
                       changes={"before": before, "after": after})
        return result

    @require_permission("products.view")
    def get_product(self, product_id: uuid.UUID) -> ProductOut:
        result = self._products.get_by_id(self._organization_id(), product_id)
        if result is None:
            raise ProductNotFoundError(product_id)
        return result

    @require_permission("products.view")
    def search_products(self, filter: ProductFilter) -> ProductPage:
        return self._products.search(self._organization_id(), filter)

    @require_permission("products.delete")
    def archive_product(self, product_id: uuid.UUID) -> None:
        self._products.set_status(self._organization_id(), product_id, ProductStatus.ARCHIVED)
        self._audit(action="product.archive", entity_id=product_id)

    @require_permission("products.update")
    def restore_product(self, product_id: uuid.UUID) -> None:
        self._products.set_status(self._organization_id(), product_id, ProductStatus.ACTIVE)
        self._audit(action="product.restore", entity_id=product_id)

    @staticmethod
    def is_available_for_transactions(product: ProductOut) -> bool:
        """Archived products must not be used for new transactions — the
        one place that rule lives, so a future Sales/Purchases product
        picker calls this instead of re-deriving it (or forgetting it).
        """
        return product.status == ProductStatus.ACTIVE
