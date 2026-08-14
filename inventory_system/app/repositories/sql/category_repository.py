"""SQLAlchemy-backed CategoryRepository — real implementation, no excel/
equivalent (categories are a new capability the legacy app never had).
"""
import uuid

from app.database.session import get_session
from app.models import Category
from app.schemas.product import CategoryCreate, CategoryOut


def _to_out(category: Category) -> CategoryOut:
    return CategoryOut(id=category.id, name=category.name, description=category.description)


class SqlCategoryRepository:
    def create(self, organization_id: uuid.UUID, data: CategoryCreate) -> CategoryOut:
        with get_session() as db:
            category = Category(organization_id=organization_id, name=data.name,
                               description=data.description)
            db.add(category)
            db.flush()
            return _to_out(category)

    def update(self, organization_id: uuid.UUID, category_id: uuid.UUID,
              data: CategoryCreate) -> CategoryOut | None:
        with get_session() as db:
            category = db.get(Category, category_id)
            if category is None or category.organization_id != organization_id:
                return None
            category.name = data.name
            category.description = data.description
            db.flush()
            return _to_out(category)

    def delete(self, organization_id: uuid.UUID, category_id: uuid.UUID) -> None:
        with get_session() as db:
            category = db.get(Category, category_id)
            if category is not None and category.organization_id == organization_id:
                db.delete(category)

    def list_all(self, organization_id: uuid.UUID) -> list[CategoryOut]:
        with get_session() as db:
            rows = (db.query(Category).filter_by(organization_id=organization_id)
                   .order_by(Category.name).all())
            return [_to_out(c) for c in rows]

    def get_by_id(self, organization_id: uuid.UUID,
                 category_id: uuid.UUID) -> CategoryOut | None:
        with get_session() as db:
            category = db.get(Category, category_id)
            if category is None or category.organization_id != organization_id:
                return None
            return _to_out(category)
