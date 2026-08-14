"""SQLAlchemy-backed SupplierRepository — real implementation, no excel/
equivalent (suppliers, distinct from the legacy Excel "Party" concept, are
a new capability this module introduces).
"""
import uuid

from app.database.session import get_session
from app.models import Supplier
from app.schemas.purchasing import SupplierCreate, SupplierOut, SupplierUpdate


def _to_out(supplier: Supplier) -> SupplierOut:
    return SupplierOut(id=supplier.id, name=supplier.name,
                       contact_person=supplier.contact_person, phone=supplier.phone,
                       email=supplier.email, address=supplier.address,
                       tax_id=supplier.tax_id, notes=supplier.notes,
                       is_active=supplier.is_active, created_at=supplier.created_at,
                       updated_at=supplier.updated_at)


class SqlSupplierRepository:
    def create(self, organization_id: uuid.UUID, data: SupplierCreate) -> SupplierOut:
        with get_session() as db:
            supplier = Supplier(organization_id=organization_id, name=data.name,
                                contact_person=data.contact_person, phone=data.phone,
                                email=data.email, address=data.address, tax_id=data.tax_id,
                                notes=data.notes)
            db.add(supplier)
            db.flush()
            return _to_out(supplier)

    def update(self, organization_id: uuid.UUID, supplier_id: uuid.UUID,
              data: SupplierUpdate) -> SupplierOut | None:
        with get_session() as db:
            supplier = db.get(Supplier, supplier_id)
            if supplier is None or supplier.organization_id != organization_id:
                return None
            for field, value in data.model_dump(exclude_unset=True).items():
                setattr(supplier, field, value)
            db.flush()
            return _to_out(supplier)

    def get_by_id(self, organization_id: uuid.UUID,
                 supplier_id: uuid.UUID) -> SupplierOut | None:
        with get_session() as db:
            supplier = db.get(Supplier, supplier_id)
            if supplier is None or supplier.organization_id != organization_id:
                return None
            return _to_out(supplier)

    def list_all(self, organization_id: uuid.UUID) -> list[SupplierOut]:
        with get_session() as db:
            rows = (db.query(Supplier).filter_by(organization_id=organization_id)
                   .order_by(Supplier.name).all())
            return [_to_out(s) for s in rows]
