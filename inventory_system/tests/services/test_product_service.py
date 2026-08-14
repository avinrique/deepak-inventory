"""ProductService tested against a hand-written fake ProductRepository — no
database. Proves validation/normalization/duplicate-detection happen in the
service (not the repository or a widget), and that product.* permissions
are enforced by calling the service directly, exactly as a UI bypass would.
"""
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest

from app.core.exceptions import (
    DuplicateBarcodeError,
    DuplicateSkuError,
    ProductNotFoundError,
    ProductValidationError,
)
from app.domain.product import ProductStatus
from app.schemas.product import ProductCreate, ProductFilter, ProductOut, ProductUpdate, UnitOut
from app.security.authorization import PermissionDeniedError
from app.security.session import SessionManager
from app.services.product_service import ProductService

UNIT_ID = uuid.uuid4()
UNIT = UnitOut(id=UNIT_ID, name="Piece", abbreviation="pc")
ORG_ID = uuid.uuid4()


class FakeProductRepository:
    def __init__(self):
        self.products: dict[uuid.UUID, ProductOut] = {}

    def create(self, organization_id, data: ProductCreate) -> ProductOut:
        from datetime import datetime, timezone
        product = ProductOut(id=uuid.uuid4(), sku=data.sku, barcode=data.barcode,
                             name=data.name, description=data.description, category=None,
                             brand=None, unit=UNIT, purchase_price=data.purchase_price,
                             selling_price=data.selling_price, tax_percent=data.tax_percent,
                             minimum_stock_level=data.minimum_stock_level,
                             status=ProductStatus.ACTIVE,
                             created_at=datetime.now(timezone.utc),
                             updated_at=datetime.now(timezone.utc))
        self.products[product.id] = product
        return product

    def update(self, organization_id, product_id, data: ProductUpdate):
        existing = self.products.get(product_id)
        if existing is None:
            return None
        updated = existing.model_copy(update=data.model_dump(exclude_unset=True))
        self.products[product_id] = updated
        return updated

    def get_by_id(self, organization_id, product_id):
        return self.products.get(product_id)

    def sku_exists(self, organization_id, sku, exclude_id=None):
        return any(p.sku == sku and p.id != exclude_id for p in self.products.values())

    def barcode_exists(self, organization_id, barcode, exclude_id=None):
        return any(p.barcode == barcode and p.id != exclude_id
                  for p in self.products.values())

    def set_status(self, organization_id, product_id, status):
        if product_id in self.products:
            self.products[product_id] = self.products[product_id].model_copy(
                update={"status": status})

    def search(self, organization_id, filter):
        raise NotImplementedError  # not exercised by these tests


def _service(permissions=frozenset({"product.create", "product.read", "product.update",
                                    "product.delete"}), repo=None):
    from datetime import datetime, timezone
    repo = repo or FakeProductRepository()
    sessions = SessionManager(idle_timeout=timedelta(minutes=30))
    sessions.start(user_id=uuid.uuid4(), organization_id=ORG_ID, role_id=uuid.uuid4(),
                   permissions=frozenset(permissions), is_superuser=False,
                   must_change_password=False, now=datetime.now(timezone.utc))
    return ProductService(repo, sessions), repo


def _create_data(**overrides):
    kwargs = dict(sku="abc-1", barcode=None, name="Widget", unit_id=UNIT_ID,
                  purchase_price=Decimal("10"), selling_price=Decimal("15"),
                  tax_percent=Decimal("13"), minimum_stock_level=Decimal("5"))
    kwargs.update(overrides)
    return ProductCreate(**kwargs)


def test_create_product_normalizes_sku_to_uppercase():
    service, repo = _service()
    created = service.create_product(_create_data(sku="  abc-1  "))
    assert created.sku == "ABC-1"


def test_create_product_normalizes_blank_barcode_to_none():
    service, _ = _service()
    created = service.create_product(_create_data(barcode="   "))
    assert created.barcode is None


def test_create_product_rejects_invalid_data():
    service, repo = _service()
    with pytest.raises(ProductValidationError):
        service.create_product(_create_data(name=""))
    assert repo.products == {}


def test_create_product_rejects_duplicate_sku():
    service, _ = _service()
    service.create_product(_create_data(sku="ABC-1"))
    with pytest.raises(DuplicateSkuError):
        service.create_product(_create_data(sku="abc-1"))  # normalizes to same SKU


def test_create_product_rejects_duplicate_barcode():
    service, _ = _service()
    service.create_product(_create_data(sku="ABC-1", barcode="111"))
    with pytest.raises(DuplicateBarcodeError):
        service.create_product(_create_data(sku="ABC-2", barcode="111"))


def test_create_product_requires_permission():
    service, repo = _service(permissions=frozenset())
    with pytest.raises(PermissionDeniedError):
        service.create_product(_create_data())
    assert repo.products == {}


def test_update_product_renormalizes_sku():
    service, _ = _service()
    created = service.create_product(_create_data(sku="ABC-1"))
    updated = service.update_product(created.id, ProductUpdate(sku="  xyz-9  "))
    assert updated.sku == "XYZ-9"


def test_update_product_rejects_duplicate_sku_from_another_product():
    service, _ = _service()
    service.create_product(_create_data(sku="ABC-1"))
    second = service.create_product(_create_data(sku="ABC-2"))
    with pytest.raises(DuplicateSkuError):
        service.update_product(second.id, ProductUpdate(sku="ABC-1"))


def test_update_product_allows_keeping_its_own_sku():
    service, _ = _service()
    created = service.create_product(_create_data(sku="ABC-1"))
    updated = service.update_product(created.id, ProductUpdate(sku="ABC-1", name="Renamed"))
    assert updated.name == "Renamed"


def test_update_product_missing_raises_not_found():
    service, _ = _service()
    with pytest.raises(ProductNotFoundError):
        service.update_product(uuid.uuid4(), ProductUpdate(name="X"))


def test_update_product_requires_permission():
    service, repo = _service(permissions={"product.create"})
    created = service.create_product(_create_data())
    other_service, _ = _service(permissions=frozenset(), repo=repo)
    with pytest.raises(PermissionDeniedError):
        other_service.update_product(created.id, ProductUpdate(name="Nope"))


def test_archive_then_restore_round_trips_status():
    service, _ = _service()
    created = service.create_product(_create_data())
    assert created.status == ProductStatus.ACTIVE

    service.archive_product(created.id)
    assert service.get_product(created.id).status == ProductStatus.ARCHIVED

    service.restore_product(created.id)
    assert service.get_product(created.id).status == ProductStatus.ACTIVE


def test_archive_requires_product_delete_permission():
    service, repo = _service(permissions={"product.create"})
    created = service.create_product(_create_data())
    other_service, _ = _service(permissions=frozenset(), repo=repo)
    with pytest.raises(PermissionDeniedError):
        other_service.archive_product(created.id)


def test_is_available_for_transactions_false_when_archived():
    service, _ = _service()
    created = service.create_product(_create_data())
    assert ProductService.is_available_for_transactions(created) is True
    service.archive_product(created.id)
    archived = service.get_product(created.id)
    assert ProductService.is_available_for_transactions(archived) is False


def test_get_product_missing_raises_not_found():
    service, _ = _service()
    with pytest.raises(ProductNotFoundError):
        service.get_product(uuid.uuid4())
