"""Application service for the current organization's own company profile
and every other Settings section (Inventory/Sales/Purchasing/Security/
Backup — see app.models.organization) — read (needed broadly, e.g. by
invoice generation, so it isn't gated behind a specific permission beyond
being logged in at all) and update (gated by settings.manage, the one
permission covering every Settings tab). This is the "Settings" the
invoice/PDF system pulls company information from — see
app.reports.sales_invoice_pdf.
"""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.core.exceptions import OrganizationNotFoundError, OrganizationValidationError
from app.repositories.interfaces import AuditLogRepository, OrganizationRepository, WarehouseRepository
from app.schemas.organization import OrganizationOut, OrganizationUpdate
from app.security.authorization import require_permission
from app.security.session import SessionManager


class OrganizationService:
    def __init__(self, organizations: OrganizationRepository, sessions: SessionManager,
                audit_log: AuditLogRepository, warehouses: WarehouseRepository):
        self._organizations = organizations
        self._sessions = sessions
        self._audit_log = audit_log
        self._warehouses = warehouses

    def _organization_id(self) -> uuid.UUID:
        return self._sessions.current(now=datetime.now(timezone.utc)).organization_id

    def get_current_organization(self) -> OrganizationOut:
        org = self._organizations.get_by_id(self._organization_id())
        if org is None:
            raise OrganizationNotFoundError(self._organization_id())
        return org

    def get_logo(self) -> tuple[bytes, str | None] | None:
        return self._organizations.get_logo(self._organization_id())

    def _validate(self, data: OrganizationUpdate, org_id: uuid.UUID) -> list[str]:
        errors = []
        if data.name is not None and not data.name.strip():
            errors.append("Organization name is required.")
        if data.invoice_number_prefix is not None and not data.invoice_number_prefix.strip():
            errors.append("Invoice number prefix cannot be blank.")
        if data.purchase_number_prefix is not None and not data.purchase_number_prefix.strip():
            errors.append("Purchase number prefix cannot be blank.")
        if data.default_tax_percent is not None and not (
                Decimal("0") <= data.default_tax_percent <= Decimal("100")):
            errors.append("Default tax percent must be between 0 and 100.")
        if data.default_discount_percent is not None and not (
                Decimal("0") <= data.default_discount_percent <= Decimal("100")):
            errors.append("Default discount percent must be between 0 and 100.")
        if data.session_timeout_minutes is not None and data.session_timeout_minutes <= 0:
            errors.append("Session timeout must be greater than zero minutes.")
        if data.password_min_length is not None and data.password_min_length < 4:
            errors.append("Minimum password length must be at least 4 characters.")
        if data.backup_retention_count is not None and data.backup_retention_count < 0:
            errors.append("Backup retention count cannot be negative.")
        if data.default_warehouse_id is not None:
            warehouse = self._warehouses.get_by_id(org_id, data.default_warehouse_id)
            if warehouse is None:
                errors.append("Default warehouse not found.")
        return errors

    @require_permission("settings.manage")
    def update_organization(self, data: OrganizationUpdate) -> OrganizationOut:
        org_id = self._organization_id()
        errors = self._validate(data, org_id)
        if errors:
            raise OrganizationValidationError(errors)

        existing = self._organizations.get_by_id(org_id)
        if existing is None:
            raise OrganizationNotFoundError(org_id)

        result = self._organizations.update(org_id, data)
        if result is None:
            raise OrganizationNotFoundError(org_id)

        # Only the fields the caller actually set, and only where the value
        # genuinely changed — a before/after diff, not a full-record dump.
        # logo_image is excluded from the diff: dumping raw image bytes
        # into an audit log entry would be both useless to read and
        # needlessly bloat the audit_logs table.
        before, after = {}, {}
        for field in data.model_dump(exclude_unset=True):
            if field == "logo_image":
                continue
            old_value, new_value = getattr(existing, field), getattr(result, field)
            if old_value != new_value:
                before[field] = str(old_value)
                after[field] = str(new_value)
        if before:
            session = self._sessions.peek()
            self._audit_log.record(
                organization_id=org_id, user_id=session.user_id if session else None,
                actor_email=None, organization_name=result.name, action="settings.update",
                entity_type="organization", entity_id=org_id,
                changes={"before": before, "after": after})

        # Applied live so a timeout change takes effect for the current
        # session immediately — see SessionManager.set_idle_timeout.
        if "session_timeout_minutes" in before:
            self._sessions.set_idle_timeout(timedelta(minutes=result.session_timeout_minutes))

        return result
