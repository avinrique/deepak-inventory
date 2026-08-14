"""OrganizationService tested against a hand-written fake repository — no
database. Proves get_current_organization needs only a valid session (not
a specific permission — it's needed broadly, e.g. by invoice generation)
while update_organization is gated by settings.manage, and that basic
validation happens in the service.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.core.exceptions import OrganizationNotFoundError, OrganizationValidationError
from app.schemas.organization import OrganizationOut, OrganizationUpdate
from app.security.authorization import PermissionDeniedError
from app.security.session import SessionManager
from app.services.organization_service import OrganizationService

ORG_ID = uuid.uuid4()


class FakeOrganizationRepository:
    def __init__(self):
        now = datetime.now(timezone.utc)
        self.org = OrganizationOut(id=ORG_ID, name="Acme Co", legal_name=None, tax_id=None,
                                   address=None, phone=None, email=None, website=None,
                                   is_active=True, allow_negative_stock=False,
                                   invoice_number_prefix="INV-", created_at=now,
                                   updated_at=now)

    def get_by_id(self, organization_id):
        return self.org if organization_id == self.org.id else None

    def update(self, organization_id, data: OrganizationUpdate):
        if organization_id != self.org.id:
            return None
        self.org = self.org.model_copy(update=data.model_dump(exclude_unset=True))
        return self.org


def _service(permissions=frozenset({"settings.manage"})):
    repo = FakeOrganizationRepository()
    sessions = SessionManager(idle_timeout=timedelta(minutes=30))
    sessions.start(user_id=uuid.uuid4(), organization_id=ORG_ID, role_id=uuid.uuid4(),
                   permissions=frozenset(permissions), is_superuser=False,
                   must_change_password=False, now=datetime.now(timezone.utc))
    return OrganizationService(repo, sessions), repo


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


def test_update_organization_missing_raises_not_found():
    service, repo = _service()
    repo.org = repo.org.model_copy(update={"id": uuid.uuid4()})  # simulate a vanished org
    with pytest.raises(OrganizationNotFoundError):
        service.update_organization(OrganizationUpdate(name="X"))
