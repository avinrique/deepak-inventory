"""OrganizationService tested against a hand-written fake repository — no
database. Proves get_current_organization needs only a valid session (not
a specific permission — it's needed broadly, e.g. by invoice generation)
while update_organization is gated by settings.manage, and that basic
validation happens in the service.
"""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.core.exceptions import OrganizationNotFoundError, OrganizationValidationError
from app.domain.backup import BackupFrequency
from app.domain.inventory import LowStockBehavior, StockValuationMethod
from app.schemas.inventory import WarehouseOut
from app.schemas.organization import OrganizationOut, OrganizationUpdate
from app.security.authorization import PermissionDeniedError
from app.security.session import SessionManager
from app.services.organization_service import OrganizationService

ORG_ID = uuid.uuid4()
WAREHOUSE_ID = uuid.uuid4()


def _default_org(**overrides) -> OrganizationOut:
    now = datetime.now(timezone.utc)
    kwargs = dict(
        id=ORG_ID, name="Acme Co", legal_name=None, tax_id=None, address=None, phone=None,
        email=None, website=None, is_active=True, logo_content_type=None, has_logo=False,
        allow_negative_stock=False, default_warehouse_id=None,
        low_stock_behavior=LowStockBehavior.WARN_ONLY,
        stock_valuation_method=StockValuationMethod.WEIGHTED_AVERAGE,
        invoice_number_prefix="INV-", default_tax_percent=Decimal("0"),
        default_discount_percent=Decimal("0"), purchase_number_prefix="PO-",
        session_timeout_minutes=30, password_min_length=8,
        password_require_uppercase=False, password_require_number=False,
        password_require_special_char=False, backup_directory=None,
        backup_frequency=BackupFrequency.MANUAL, backup_retention_count=None,
        created_at=now, updated_at=now)
    kwargs.update(overrides)
    return OrganizationOut(**kwargs)


class FakeOrganizationRepository:
    def __init__(self):
        self.org = _default_org()

    def get_by_id(self, organization_id):
        return self.org if organization_id == self.org.id else None

    def update(self, organization_id, data: OrganizationUpdate):
        if organization_id != self.org.id:
            return None
        self.org = self.org.model_copy(update=data.model_dump(exclude_unset=True))
        return self.org

    def get_logo(self, organization_id):
        return None


class FakeWarehouseRepository:
    """Only WAREHOUSE_ID (under ORG_ID) resolves — anything else is "not
    found", the same as a real, wrong, or foreign-org warehouse id.
    """
    def get_by_id(self, organization_id, warehouse_id):
        if organization_id == ORG_ID and warehouse_id == WAREHOUSE_ID:
            now = datetime.now(timezone.utc)
            return WarehouseOut(id=WAREHOUSE_ID, code="MAIN", name="Main", address=None,
                                is_active=True, created_at=now, updated_at=now)
        return None


class FakeAuditLogRepository:
    def __init__(self):
        self.entries: list[dict] = []

    def record(self, *, organization_id, user_id, actor_email, organization_name, action,
              entity_type=None, entity_id=None, changes=None):
        self.entries.append({"organization_id": organization_id, "user_id": user_id,
                            "actor_email": actor_email, "organization_name": organization_name,
                            "action": action, "entity_type": entity_type,
                            "entity_id": entity_id, "changes": changes})


def _service(permissions=frozenset({"settings.manage"}), audit_log=None):
    repo = FakeOrganizationRepository()
    audit_log = audit_log if audit_log is not None else FakeAuditLogRepository()
    sessions = SessionManager(idle_timeout=timedelta(minutes=30))
    sessions.start(user_id=uuid.uuid4(), organization_id=ORG_ID, role_id=uuid.uuid4(),
                   permissions=frozenset(permissions), is_superuser=False,
                   must_change_password=False, now=datetime.now(timezone.utc))
    return OrganizationService(repo, sessions, audit_log, FakeWarehouseRepository()), repo


def test_get_current_organization_needs_no_specific_permission():
    service, _ = _service(permissions=frozenset())
    org = service.get_current_organization()
    assert org.name == "Acme Co"


def test_update_organization_requires_settings_manage():
    service, _ = _service(permissions=frozenset())
    with pytest.raises(PermissionDeniedError):
        service.update_organization(OrganizationUpdate(name="New Name"))


def test_update_organization_persists_changes():
    service, _ = _service()
    updated = service.update_organization(OrganizationUpdate(
        phone="+1-555-0100", email="hi@acme.example"))
    assert updated.phone == "+1-555-0100"
    assert updated.email == "hi@acme.example"
    assert updated.name == "Acme Co"   # untouched


def test_update_organization_rejects_blank_name():
    service, _ = _service()
    with pytest.raises(OrganizationValidationError):
        service.update_organization(OrganizationUpdate(name="   "))


def test_update_organization_rejects_blank_invoice_prefix():
    service, _ = _service()
    with pytest.raises(OrganizationValidationError):
        service.update_organization(OrganizationUpdate(invoice_number_prefix="  "))


def test_update_organization_rejects_blank_purchase_prefix():
    service, _ = _service()
    with pytest.raises(OrganizationValidationError):
        service.update_organization(OrganizationUpdate(purchase_number_prefix="  "))


def test_update_organization_rejects_malformed_email():
    """Regression test: Organization.email/phone had no format validation
    at all, so garbage values could silently flow into every generated
    invoice PDF (company_email/phone) — see
    app.reports.sales_invoice_pdf.
    """
    service, _ = _service()
    with pytest.raises(OrganizationValidationError):
        service.update_organization(OrganizationUpdate(email="not-an-email"))


def test_update_organization_accepts_valid_email():
    service, repo = _service()
    result = service.update_organization(OrganizationUpdate(email="billing@acme.test"))
    assert result.email == "billing@acme.test"


def test_update_organization_rejects_malformed_phone():
    service, _ = _service()
    with pytest.raises(OrganizationValidationError):
        service.update_organization(OrganizationUpdate(phone="not a phone number!!"))


def test_update_organization_rejects_out_of_range_default_tax_percent():
    service, _ = _service()
    with pytest.raises(OrganizationValidationError):
        service.update_organization(OrganizationUpdate(default_tax_percent=Decimal("150")))


def test_update_organization_rejects_zero_session_timeout():
    service, _ = _service()
    with pytest.raises(OrganizationValidationError):
        service.update_organization(OrganizationUpdate(session_timeout_minutes=0))


def test_update_organization_rejects_password_min_length_below_floor():
    service, _ = _service()
    with pytest.raises(OrganizationValidationError):
        service.update_organization(OrganizationUpdate(password_min_length=2))


def test_update_organization_rejects_negative_backup_retention():
    service, _ = _service()
    with pytest.raises(OrganizationValidationError):
        service.update_organization(OrganizationUpdate(backup_retention_count=-1))


def test_update_organization_accepts_known_default_warehouse():
    service, _ = _service()
    updated = service.update_organization(
        OrganizationUpdate(default_warehouse_id=WAREHOUSE_ID))
    assert updated.default_warehouse_id == WAREHOUSE_ID


def test_update_organization_rejects_unknown_default_warehouse():
    service, _ = _service()
    with pytest.raises(OrganizationValidationError):
        service.update_organization(OrganizationUpdate(default_warehouse_id=uuid.uuid4()))


def test_update_organization_applies_session_timeout_live(monkeypatch):
    service, _ = _service()
    applied = []
    monkeypatch.setattr(service._sessions, "set_idle_timeout", lambda td: applied.append(td))

    service.update_organization(OrganizationUpdate(session_timeout_minutes=90))

    assert applied == [timedelta(minutes=90)]


def test_update_organization_missing_raises_not_found():
    service, repo = _service()
    repo.org = repo.org.model_copy(update={"id": uuid.uuid4()})  # simulate a vanished org
    with pytest.raises(OrganizationNotFoundError):
        service.update_organization(OrganizationUpdate(name="X"))
