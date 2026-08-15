"""SQLAlchemy-backed BrandRepository — mirrors category_repository.py."""
import uuid

from sqlalchemy.exc import IntegrityError

from app.core.exceptions import DuplicateBrandNameError
from app.database.session import get_session
from app.models import Brand
from app.repositories.sql.constraint_utils import constraint_name
from app.schemas.product import BrandCreate, BrandOut

_UNIQUE_NAME_CONSTRAINT = "ix_brands_org_name"


def _to_out(brand: Brand) -> BrandOut:
    return BrandOut(id=brand.id, name=brand.name, description=brand.description)


class SqlBrandRepository:
    def create(self, organization_id: uuid.UUID, data: BrandCreate) -> BrandOut:
        with get_session() as db:
            brand = Brand(organization_id=organization_id, name=data.name,
                         description=data.description)
            db.add(brand)
            try:
                db.flush()
            except IntegrityError as exc:
                if constraint_name(exc) == _UNIQUE_NAME_CONSTRAINT:
                    raise DuplicateBrandNameError(data.name) from exc
                raise
            return _to_out(brand)

    def update(self, organization_id: uuid.UUID, brand_id: uuid.UUID,
              data: BrandCreate) -> BrandOut | None:
        with get_session() as db:
            brand = db.get(Brand, brand_id)
            if brand is None or brand.organization_id != organization_id:
                return None
            brand.name = data.name
            brand.description = data.description
            try:
                db.flush()
            except IntegrityError as exc:
                if constraint_name(exc) == _UNIQUE_NAME_CONSTRAINT:
                    raise DuplicateBrandNameError(data.name) from exc
                raise
            return _to_out(brand)

    def delete(self, organization_id: uuid.UUID, brand_id: uuid.UUID) -> None:
        with get_session() as db:
            brand = db.get(Brand, brand_id)
            if brand is not None and brand.organization_id == organization_id:
                db.delete(brand)

    def list_all(self, organization_id: uuid.UUID) -> list[BrandOut]:
        with get_session() as db:
            rows = (db.query(Brand).filter_by(organization_id=organization_id)
                   .order_by(Brand.name).all())
            return [_to_out(b) for b in rows]

    def get_by_id(self, organization_id: uuid.UUID, brand_id: uuid.UUID) -> BrandOut | None:
        with get_session() as db:
            brand = db.get(Brand, brand_id)
            if brand is None or brand.organization_id != organization_id:
                return None
            return _to_out(brand)
