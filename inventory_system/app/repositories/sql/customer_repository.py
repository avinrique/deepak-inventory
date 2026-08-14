"""SQLAlchemy-backed CustomerRepository — real implementation, no excel/
equivalent (customers, distinct from the legacy Excel "Party" concept, are
a new capability this module introduces — see SupplierRepository for the
purchasing-side analog).
"""
import uuid

from app.database.session import get_session
from app.models import Customer
from app.schemas.sales import CustomerCreate, CustomerOut, CustomerUpdate


def _to_out(customer: Customer) -> CustomerOut:
    return CustomerOut(id=customer.id, name=customer.name,
                       contact_person=customer.contact_person, phone=customer.phone,
                       email=customer.email, address=customer.address,
                       tax_id=customer.tax_id, notes=customer.notes,
                       is_active=customer.is_active, created_at=customer.created_at,
                       updated_at=customer.updated_at)


class SqlCustomerRepository:
    def create(self, organization_id: uuid.UUID, data: CustomerCreate) -> CustomerOut:
        with get_session() as db:
            customer = Customer(organization_id=organization_id, name=data.name,
                                contact_person=data.contact_person, phone=data.phone,
                                email=data.email, address=data.address, tax_id=data.tax_id,
                                notes=data.notes)
            db.add(customer)
            db.flush()
            return _to_out(customer)

    def update(self, organization_id: uuid.UUID, customer_id: uuid.UUID,
              data: CustomerUpdate) -> CustomerOut | None:
        with get_session() as db:
            customer = db.get(Customer, customer_id)
            if customer is None or customer.organization_id != organization_id:
                return None
            for field, value in data.model_dump(exclude_unset=True).items():
                setattr(customer, field, value)
            db.flush()
            return _to_out(customer)

    def get_by_id(self, organization_id: uuid.UUID,
                 customer_id: uuid.UUID) -> CustomerOut | None:
        with get_session() as db:
            customer = db.get(Customer, customer_id)
            if customer is None or customer.organization_id != organization_id:
                return None
            return _to_out(customer)

    def list_all(self, organization_id: uuid.UUID) -> list[CustomerOut]:
        with get_session() as db:
            rows = (db.query(Customer).filter_by(organization_id=organization_id)
                   .order_by(Customer.name).all())
            return [_to_out(c) for c in rows]
