"""Application service for Product CRUD, archive/restore, and search.

Validation/normalization lives in app.domain.product (pure, no I/O); this
orchestrates that plus ProductRepository — the one seam the UI is allowed
to call. organization_id always comes from the current session, never from
a caller-supplied argument, so a product operation can't be pointed at a
different tenant's catalog.
"""
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from app.core.exceptions import (
    DuplicateBarcodeError,
    DuplicateSkuError,
    ProductNotFoundError,
    ProductValidationError,
    UnitNotFoundError,
    WarehouseNotFoundError,
)
from app.domain.product import (
    ProductStatus,
    normalize_barcode,
    normalize_optional_text,
    normalize_sku,
    validate_opening_stock,
    validate_product,
)
from app.repositories.interfaces import (
    AuditLogRepository,
    ProductRepository,
    UnitRepository,
    WarehouseRepository,
)
from app.schemas.inventory import InventoryTransactionOut
from app.schemas.product import ProductCreate, ProductFilter, ProductOut, ProductPage, ProductUpdate
from app.security.authorization import check_permission, require_permission
from app.security.session import SessionManager


class ProductService:
    def __init__(self, products: ProductRepository, sessions: SessionManager,
                audit_log: AuditLogRepository, warehouses: WarehouseRepository,
                units: UnitRepository):
        self._products = products
        self._sessions = sessions
        self._audit_log = audit_log
        self._warehouses = warehouses
        self._units = units

    def _organization_id(self) -> uuid.UUID:
        return self._sessions.current(now=datetime.now(timezone.utc)).organization_id

    def _audit(self, *, action: str, entity_id: uuid.UUID, changes: dict | None = None) -> None:
        session = self._sessions.peek()
        actor_id = session.user_id if session else None
        self._audit_log.record(organization_id=self._organization_id(), user_id=actor_id,
                               actor_email=None, organization_name=None, action=action,
                               entity_type="product", entity_id=entity_id, changes=changes)

    def _require_unit(self, org_id: uuid.UUID, unit_id: uuid.UUID | None,
                      field_label: str) -> list[str]:
        """Friendly, service-layer existence check for a unit reference —
        without this, an invalid/foreign unit_id would only be caught by
        the database FK constraint, surfacing as a raw IntegrityError
        instead of a clear message (see this module's docstring on never
        exposing raw SQL/Python errors to the UI).
        """
        if unit_id is None:
            return []
        if self._units.get_by_id(org_id, unit_id) is None:
            return [f"{field_label} not found."]
        return []

    @require_permission("products.create")
    def create_product(self, data: ProductCreate, *, warehouse_id: uuid.UUID | None = None,
                       opening_quantity: Decimal = Decimal("0")) -> tuple[
                           ProductOut, InventoryTransactionOut | None]:
        """Creates the product and, if opening_quantity > 0, its opening
        Inventory record + InventoryTransaction at warehouse_id — as ONE
        database transaction (SqlProductRepository.create_with_opening_
        stock): either both land or neither does, never a product with no
        matching stock record or a stock record with no product. Returns
        (product, opening_stock_transaction_or_None).
        """
        session = self._sessions.current(now=datetime.now(timezone.utc))
        org_id = session.organization_id
        # Creating a product never needs inventory.adjust on its own — the
        # extra check only kicks in when the caller is actually asking to
        # move stock (a nonzero opening quantity), same convention as
        # SalesService.finalize_new_bill's per-sub-action permission checks.
        if opening_quantity > 0:
            check_permission(session, "inventory.adjust")

        sku = normalize_sku(data.sku)
        barcode = normalize_barcode(data.barcode)
        hsn_code = normalize_optional_text(data.hsn_code)
        size = normalize_optional_text(data.size)
        color = normalize_optional_text(data.color)
        flavour = normalize_optional_text(data.flavour)
        dftqc_no = normalize_optional_text(data.dftqc_no)
        country_of_origin = normalize_optional_text(data.country_of_origin)

        errors = validate_product(
            sku=sku, name=data.name, purchase_price=data.purchase_price,
            selling_price=data.selling_price, tax_percent=data.tax_percent,
            minimum_stock_level=data.minimum_stock_level, is_taxable=data.is_taxable,
            unit_id=data.unit_id, sub_unit_id=data.sub_unit_id,
            sub_unit_conversion_factor=data.sub_unit_conversion_factor,
            tertiary_unit_id=data.tertiary_unit_id,
            tertiary_unit_conversion_factor=data.tertiary_unit_conversion_factor,
            expiry_date=data.expiry_date)
        errors += validate_opening_stock(opening_quantity=opening_quantity,
                                         warehouse_id=warehouse_id,
                                         product_type=data.product_type)
        errors += self._require_unit(org_id, data.unit_id, "Unit")
        errors += self._require_unit(org_id, data.sub_unit_id, "Sub-unit")
        errors += self._require_unit(org_id, data.tertiary_unit_id, "Tertiary unit")
        if errors:
            raise ProductValidationError(errors)

        if self._products.sku_exists(org_id, sku):
            raise DuplicateSkuError(sku)
        if barcode and self._products.barcode_exists(org_id, barcode):
            raise DuplicateBarcodeError(barcode)
        if warehouse_id is not None:
            warehouse = self._warehouses.get_by_id(org_id, warehouse_id)
            if warehouse is None:
                raise WarehouseNotFoundError(warehouse_id)

        normalized = data.model_copy(update={
            "sku": sku, "barcode": barcode, "hsn_code": hsn_code, "size": size, "color": color,
            "flavour": flavour, "dftqc_no": dftqc_no, "country_of_origin": country_of_origin})
        created, opening_transaction = self._products.create_with_opening_stock(
            org_id, normalized, warehouse_id, opening_quantity, session.user_id)

        changes = {"sku": created.sku, "name": created.name,
                  "product_type": created.product_type.value,
                  "purchase_price": str(created.purchase_price),
                  "selling_price": str(created.selling_price)}
        if opening_transaction is not None:
            changes["opening_quantity"] = str(opening_quantity)
            changes["opening_warehouse_id"] = str(warehouse_id)
        self._audit(action="product.create", entity_id=created.id, changes=changes)
        return created, opening_transaction

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
        is_taxable = updates.get("is_taxable", existing.is_taxable)
        minimum_stock_level = updates.get("minimum_stock_level", existing.minimum_stock_level)
        unit_id = updates.get("unit_id", existing.unit.id)
        sub_unit_id = (updates["sub_unit_id"] if "sub_unit_id" in updates
                      else (existing.sub_unit.id if existing.sub_unit else None))
        sub_unit_conversion_factor = updates.get(
            "sub_unit_conversion_factor", existing.sub_unit_conversion_factor)
        tertiary_unit_id = (updates["tertiary_unit_id"] if "tertiary_unit_id" in updates
                           else (existing.tertiary_unit.id if existing.tertiary_unit else None))
        tertiary_unit_conversion_factor = updates.get(
            "tertiary_unit_conversion_factor", existing.tertiary_unit_conversion_factor)
        expiry_date = updates.get("expiry_date", existing.expiry_date)

        for text_field in ("hsn_code", "size", "color", "flavour", "dftqc_no",
                          "country_of_origin"):
            if text_field in updates:
                updates[text_field] = normalize_optional_text(updates[text_field])

        errors = validate_product(
            sku=sku, name=name, purchase_price=purchase_price, selling_price=selling_price,
            tax_percent=tax_percent, minimum_stock_level=minimum_stock_level,
            is_taxable=is_taxable, unit_id=unit_id, sub_unit_id=sub_unit_id,
            sub_unit_conversion_factor=sub_unit_conversion_factor,
            tertiary_unit_id=tertiary_unit_id,
            tertiary_unit_conversion_factor=tertiary_unit_conversion_factor,
            expiry_date=expiry_date)
        if "unit_id" in updates:
            errors += self._require_unit(org_id, updates["unit_id"], "Unit")
        if "sub_unit_id" in updates:
            errors += self._require_unit(org_id, updates["sub_unit_id"], "Sub-unit")
        if "tertiary_unit_id" in updates:
            errors += self._require_unit(org_id, updates["tertiary_unit_id"], "Tertiary unit")
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
