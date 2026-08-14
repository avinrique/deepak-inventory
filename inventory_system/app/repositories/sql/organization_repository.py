"""SQLAlchemy-backed OrganizationRepository — read/update the current
tenant's own company profile, plus every other Settings section (see
app.models.organization's module docstring). No create/delete:
organizations are created during signup/seeding (see scripts/init_db.py),
not through this repository.
"""
import uuid

from app.database.session import get_session
from app.models import Organization
from app.schemas.organization import OrganizationOut, OrganizationUpdate


def _to_out(org: Organization) -> OrganizationOut:
    return OrganizationOut(
        id=org.id, name=org.name, legal_name=org.legal_name, tax_id=org.tax_id,
        address=org.address, phone=org.phone, email=org.email, website=org.website,
        is_active=org.is_active, logo_content_type=org.logo_content_type,
        has_logo=org.logo_image is not None,
        allow_negative_stock=org.allow_negative_stock,
        default_warehouse_id=org.default_warehouse_id,
        low_stock_behavior=org.low_stock_behavior,
        stock_valuation_method=org.stock_valuation_method,
        invoice_number_prefix=org.invoice_number_prefix,
        default_tax_percent=org.default_tax_percent,
        default_discount_percent=org.default_discount_percent,
        purchase_number_prefix=org.purchase_number_prefix,
        session_timeout_minutes=org.session_timeout_minutes,
        password_min_length=org.password_min_length,
        password_require_uppercase=org.password_require_uppercase,
        password_require_number=org.password_require_number,
        password_require_special_char=org.password_require_special_char,
        backup_directory=org.backup_directory, backup_frequency=org.backup_frequency,
        backup_retention_count=org.backup_retention_count,
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

    def get_logo(self, organization_id: uuid.UUID) -> tuple[bytes, str | None] | None:
        """Kept out of OrganizationOut/get_by_id on purpose — every other
        Settings read (invoice generation, the Settings page's own load)
        would otherwise drag a potentially large binary blob along with it
        every time. Callers that actually need to render the logo (the
        Company tab's preview, a future invoice-with-logo renderer) fetch
        it separately, only when they need it.
        """
        with get_session() as db:
            org = db.get(Organization, organization_id)
            if org is None or org.logo_image is None:
                return None
            return org.logo_image, org.logo_content_type
