"""CatalogService (Category/Brand/Unit) tested against fake repositories —
no database. Mainly proves product.* permission enforcement, since the
CRUD itself is a thin passthrough to the repository.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.schemas.product import BrandCreate, CategoryCreate, UnitCreate
from app.security.authorization import PermissionDeniedError
from app.security.session import SessionManager
from app.services.catalog_service import CatalogService

ORG_ID = uuid.uuid4()


class FakeCategoryRepository:
    def __init__(self):
        self.created = []

    def create(self, organization_id, data):
        self.created.append((organization_id, data))
        return object()

    def update(self, organization_id, category_id, data):
        return object()

    def delete(self, organization_id, category_id):
        pass

    def list_all(self, organization_id):
        return []

    def get_by_id(self, organization_id, category_id):
        return None


class FakeBrandRepository(FakeCategoryRepository):
    pass


class FakeUnitRepository(FakeCategoryRepository):
    pass


def _service(permissions):
    sessions = SessionManager(idle_timeout=timedelta(minutes=30))
    sessions.start(user_id=uuid.uuid4(), organization_id=ORG_ID, role_id=uuid.uuid4(),
                   permissions=frozenset(permissions), is_superuser=False,
                   must_change_password=False, now=datetime.now(timezone.utc))
    categories, brands, units = (FakeCategoryRepository(), FakeBrandRepository(),
                                 FakeUnitRepository())
    return CatalogService(categories, brands, units, sessions), categories, brands, units


def test_create_category_requires_product_create_permission():
    service, categories, _, _ = _service(frozenset())
    with pytest.raises(PermissionDeniedError):
        service.create_category(CategoryCreate(name="Beverages"))
    assert categories.created == []


def test_create_category_succeeds_with_permission():
    service, categories, _, _ = _service({"products.create"})
    service.create_category(CategoryCreate(name="Beverages"))
    assert len(categories.created) == 1
    assert categories.created[0][0] == ORG_ID


def test_list_categories_requires_product_read_permission():
    service, _, _, _ = _service(frozenset())
    with pytest.raises(PermissionDeniedError):
        service.list_categories()


def test_delete_brand_requires_product_delete_permission():
    service, _, brands, _ = _service({"products.create"})
    with pytest.raises(PermissionDeniedError):
        service.delete_brand(uuid.uuid4())


def test_create_brand_succeeds_with_permission():
    service, _, brands, _ = _service({"products.create"})
    service.create_brand(BrandCreate(name="Acme"))
    assert len(brands.created) == 1


def test_create_unit_succeeds_with_permission():
    service, _, _, units = _service({"products.create"})
    service.create_unit(UnitCreate(name="Kilogram", abbreviation="kg"))
    assert len(units.created) == 1


def test_update_unit_requires_product_update_permission():
    service, _, _, _ = _service(frozenset())
    with pytest.raises(PermissionDeniedError):
        service.update_unit(uuid.uuid4(), UnitCreate(name="Kilogram", abbreviation="kg"))
