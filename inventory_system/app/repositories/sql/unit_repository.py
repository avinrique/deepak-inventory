"""SQLAlchemy-backed UnitRepository — mirrors category_repository.py."""
import uuid

from app.database.session import get_session
from app.models import Unit
from app.schemas.product import UnitCreate, UnitOut


def _to_out(unit: Unit) -> UnitOut:
    return UnitOut(id=unit.id, name=unit.name, abbreviation=unit.abbreviation)


class SqlUnitRepository:
    def create(self, organization_id: uuid.UUID, data: UnitCreate) -> UnitOut:
        with get_session() as db:
            unit = Unit(organization_id=organization_id, name=data.name,
                       abbreviation=data.abbreviation)
            db.add(unit)
            db.flush()
            return _to_out(unit)

    def update(self, organization_id: uuid.UUID, unit_id: uuid.UUID,
              data: UnitCreate) -> UnitOut | None:
        with get_session() as db:
            unit = db.get(Unit, unit_id)
            if unit is None or unit.organization_id != organization_id:
                return None
            unit.name = data.name
            unit.abbreviation = data.abbreviation
            db.flush()
            return _to_out(unit)

    def delete(self, organization_id: uuid.UUID, unit_id: uuid.UUID) -> None:
        with get_session() as db:
            unit = db.get(Unit, unit_id)
            if unit is not None and unit.organization_id == organization_id:
                db.delete(unit)

    def list_all(self, organization_id: uuid.UUID) -> list[UnitOut]:
        with get_session() as db:
            rows = (db.query(Unit).filter_by(organization_id=organization_id)
                   .order_by(Unit.name).all())
            return [_to_out(u) for u in rows]

    def get_by_id(self, organization_id: uuid.UUID, unit_id: uuid.UUID) -> UnitOut | None:
        with get_session() as db:
            unit = db.get(Unit, unit_id)
            if unit is None or unit.organization_id != organization_id:
                return None
            return _to_out(unit)
