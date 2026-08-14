"""SQLAlchemy-backed WarehouseRepository — real implementation, no excel/
equivalent (warehouses are a new capability the legacy app never had).
"""
import uuid

from app.database.session import get_session
from app.models import Warehouse
from app.schemas.inventory import WarehouseCreate, WarehouseOut, WarehouseUpdate


def _to_out(warehouse: Warehouse) -> WarehouseOut:
    return WarehouseOut(id=warehouse.id, code=warehouse.code, name=warehouse.name,
                        address=warehouse.address, is_active=warehouse.is_active,
                        created_at=warehouse.created_at, updated_at=warehouse.updated_at)


class SqlWarehouseRepository:
    def create(self, organization_id: uuid.UUID, data: WarehouseCreate) -> WarehouseOut:
        with get_session() as db:
            warehouse = Warehouse(organization_id=organization_id, code=data.code,
                                  name=data.name, address=data.address)
            db.add(warehouse)
            db.flush()
            return _to_out(warehouse)

    def update(self, organization_id: uuid.UUID, warehouse_id: uuid.UUID,
              data: WarehouseUpdate) -> WarehouseOut | None:
        with get_session() as db:
            warehouse = db.get(Warehouse, warehouse_id)
            if warehouse is None or warehouse.organization_id != organization_id:
                return None
            for field, value in data.model_dump(exclude_unset=True).items():
                setattr(warehouse, field, value)
            db.flush()
            return _to_out(warehouse)

    def get_by_id(self, organization_id: uuid.UUID,
                 warehouse_id: uuid.UUID) -> WarehouseOut | None:
        with get_session() as db:
            warehouse = db.get(Warehouse, warehouse_id)
            if warehouse is None or warehouse.organization_id != organization_id:
                return None
            return _to_out(warehouse)

    def code_exists(self, organization_id: uuid.UUID, code: str,
                    exclude_id: uuid.UUID | None = None) -> bool:
        with get_session() as db:
            query = db.query(Warehouse.id).filter_by(organization_id=organization_id, code=code)
            if exclude_id is not None:
                query = query.filter(Warehouse.id != exclude_id)
            return query.first() is not None

    def list_all(self, organization_id: uuid.UUID) -> list[WarehouseOut]:
        with get_session() as db:
            rows = (db.query(Warehouse).filter_by(organization_id=organization_id)
                   .order_by(Warehouse.name).all())
            return [_to_out(w) for w in rows]
