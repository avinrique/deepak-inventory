"""Application service for the product catalog's supporting lookups —
Category, Brand, Unit. Gated by the same product.* permissions as Product
itself: they're sub-resources of "managing the product catalog," not a
distinct permission domain (see app/security/permissions.py's catalog —
adding category.manage etc. would be reasonable but wasn't asked for and
means a migration + reseed, not a free change).
"""
import uuid
from datetime import datetime, timezone

from app.repositories.interfaces import BrandRepository, CategoryRepository, UnitRepository
from app.schemas.product import BrandCreate, BrandOut, CategoryCreate, CategoryOut, UnitCreate, UnitOut
from app.security.authorization import require_permission
from app.security.session import SessionManager


class CatalogService:
    def __init__(self, categories: CategoryRepository, brands: BrandRepository,
                units: UnitRepository, sessions: SessionManager):
        self._categories = categories
        self._brands = brands
        self._units = units
        self._sessions = sessions

    def _organization_id(self) -> uuid.UUID:
        return self._sessions.current(now=datetime.now(timezone.utc)).organization_id

    # -- categories ------------------------------------------------- #
    @require_permission("products.view")
    def list_categories(self) -> list[CategoryOut]:
        return self._categories.list_all(self._organization_id())

    @require_permission("products.create")
    def create_category(self, data: CategoryCreate) -> CategoryOut:
        return self._categories.create(self._organization_id(), data)

    @require_permission("products.update")
    def update_category(self, category_id: uuid.UUID, data: CategoryCreate) -> CategoryOut | None:
        return self._categories.update(self._organization_id(), category_id, data)

    @require_permission("products.delete")
    def delete_category(self, category_id: uuid.UUID) -> None:
        self._categories.delete(self._organization_id(), category_id)

    # -- brands ------------------------------------------------------ #
    @require_permission("products.view")
    def list_brands(self) -> list[BrandOut]:
        return self._brands.list_all(self._organization_id())

    @require_permission("products.create")
    def create_brand(self, data: BrandCreate) -> BrandOut:
        return self._brands.create(self._organization_id(), data)

    @require_permission("products.update")
    def update_brand(self, brand_id: uuid.UUID, data: BrandCreate) -> BrandOut | None:
        return self._brands.update(self._organization_id(), brand_id, data)

    @require_permission("products.delete")
    def delete_brand(self, brand_id: uuid.UUID) -> None:
        self._brands.delete(self._organization_id(), brand_id)

    # -- units --------------------------------------------------------#
    @require_permission("products.view")
    def list_units(self) -> list[UnitOut]:
        return self._units.list_all(self._organization_id())

    @require_permission("products.create")
    def create_unit(self, data: UnitCreate) -> UnitOut:
        return self._units.create(self._organization_id(), data)

    @require_permission("products.update")
    def update_unit(self, unit_id: uuid.UUID, data: UnitCreate) -> UnitOut | None:
        return self._units.update(self._organization_id(), unit_id, data)

    @require_permission("products.delete")
    def delete_unit(self, unit_id: uuid.UUID) -> None:
        self._units.delete(self._organization_id(), unit_id)
