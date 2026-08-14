"""SQLAlchemy-backed OrganizationRepository — read/update the current
tenant's own company profile. No create/delete: organizations are created
during signup/seeding (see scripts/init_db.py), not through this repository.
"""
import uuid

from app.database.session import get_session
from app.models import Organization
from app.schemas.organization import OrganizationOut, OrganizationUpdate


def _to_out(org: Organization) -> OrganizationOut:
    return OrganizationOut(id=org.id, name=org.name, legal_name=org.legal_name,
                           tax_id=org.tax_id, address=org.address, phone=org.phone,
                           email=org.email, website=org.website, is_active=org.is_active,
                           allow_negative_stock=org.allow_negative_stock,
                           invoice_number_prefix=org.invoice_number_prefix,
                           created_at=org.created_at, updated_at=org.updated_at)


class SqlOrganizationRepository:
    def get_by_id(self, organization_id: uuid.UUID) -> OrganizationOut | None:
        with get_session() as db:
            org = db.get(Organization, organization_id)
            return _to_out(org) if org is not None else None

    def update(self, organization_id: uuid.UUID,
              data: OrganizationUpdate) -> OrganizationOut | None:
        with get_session() as db:
            org = db.get(Organization, organization_id)
            if org is None:
                return None
            for field, value in data.model_dump(exclude_unset=True).items():
                setattr(org, field, value)
            db.flush()
            return _to_out(org)
