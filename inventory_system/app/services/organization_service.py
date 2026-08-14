"""Application service for the current organization's own company profile
— read (needed broadly, e.g. by invoice generation, so it isn't gated
behind a specific permission beyond being logged in at all) and update
(gated by settings.manage). This is the "Settings" the invoice/PDF system
pulls company information from — see app.reports.sales_invoice_pdf.
"""
import uuid
from datetime import datetime, timezone

from app.core.exceptions import OrganizationNotFoundError, OrganizationValidationError
from app.repositories.interfaces import OrganizationRepository
from app.schemas.organization import OrganizationOut, OrganizationUpdate
from app.security.authorization import require_permission
from app.security.session import SessionManager


class OrganizationService:
    def __init__(self, organizations: OrganizationRepository, sessions: SessionManager):
        self._organizations = organizations
        self._sessions = sessions

    def _organization_id(self) -> uuid.UUID:
        return self._sessions.current(now=datetime.now(timezone.utc)).organization_id

    def get_current_organization(self) -> OrganizationOut:
        org = self._organizations.get_by_id(self._organization_id())
        if org is None:
            raise OrganizationNotFoundError(self._organization_id())
        return org

    @require_permission("settings.manage")
    def update_organization(self, data: OrganizationUpdate) -> OrganizationOut:
        if data.name is not None and not data.name.strip():
            raise OrganizationValidationError(["Organization name is required."])
        if data.invoice_number_prefix is not None and not data.invoice_number_prefix.strip():
            raise OrganizationValidationError(["Invoice number prefix cannot be blank."])
        result = self._organizations.update(self._organization_id(), data)
        if result is None:
            raise OrganizationNotFoundError(self._organization_id())
        return result
