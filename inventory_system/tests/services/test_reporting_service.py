"""ReportingService tested against a hand-written fake repository — no
database. This service is a thin permission-enforcing pass-through, so
these tests only prove reports.view is checked before every call and the
organization_id/filter reach the repository unchanged. The actual
aggregation math is proven against a live database in
tests/repositories/test_sql_reporting_repository.py.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.schemas.reporting import DashboardMetrics, ReportFilter, ReportResult
from app.security.authorization import PermissionDeniedError
from app.security.session import SessionManager
from app.services.reporting_service import ReportingService

ORG_ID = uuid.uuid4()

_METHODS = ["get_dashboard_metrics", "stock_report", "sales_report", "purchase_report",
           "profit_report", "low_stock_report", "supplier_report", "customer_report",
           "product_movement_report", "inventory_valuation_report"]


class FakeReportingRepository:
    def __init__(self):
        self.calls: list[tuple[str, uuid.UUID, ReportFilter]] = []

    def _record(self, name, organization_id, filter):
        self.calls.append((name, organization_id, filter))

    def get_dashboard_metrics(self, organization_id, filter) -> DashboardMetrics:
        self._record("get_dashboard_metrics", organization_id, filter)
        return DashboardMetrics(total_products=0, total_inventory_units=0, inventory_value=0,
                               low_stock_count=0, total_sales=0, total_purchases=0,
                               outstanding_payments=0, recent_transactions=[],
                               top_selling_products=[], sales_trend=[], purchase_trend=[],
                               inventory_by_category=[])

    def _report(self, name, organization_id, filter) -> ReportResult:
        self._record(name, organization_id, filter)
        return ReportResult(title=name, generated_at=datetime.now(timezone.utc), columns=[],
                            rows=[])

    def stock_report(self, organization_id, filter): return self._report("stock_report", organization_id, filter)
    def sales_report(self, organization_id, filter): return self._report("sales_report", organization_id, filter)
    def purchase_report(self, organization_id, filter): return self._report("purchase_report", organization_id, filter)
    def profit_report(self, organization_id, filter): return self._report("profit_report", organization_id, filter)
    def low_stock_report(self, organization_id, filter): return self._report("low_stock_report", organization_id, filter)
    def supplier_report(self, organization_id, filter): return self._report("supplier_report", organization_id, filter)
    def customer_report(self, organization_id, filter): return self._report("customer_report", organization_id, filter)
    def product_movement_report(self, organization_id, filter): return self._report("product_movement_report", organization_id, filter)
    def inventory_valuation_report(self, organization_id, filter): return self._report("inventory_valuation_report", organization_id, filter)


def _service(permissions=frozenset({"reports.view"})):
    repo = FakeReportingRepository()
    sessions = SessionManager(idle_timeout=timedelta(minutes=30))
    sessions.start(user_id=uuid.uuid4(), organization_id=ORG_ID, role_id=uuid.uuid4(),
                   permissions=frozenset(permissions), is_superuser=False,
                   must_change_password=False, now=datetime.now(timezone.utc))
    return ReportingService(repo, sessions), repo


@pytest.mark.parametrize("method_name", _METHODS)
def test_every_report_method_requires_reports_view_permission(method_name):
    service, repo = _service(permissions=frozenset())
    method = getattr(service, method_name)
    with pytest.raises(PermissionDeniedError):
        method()
    assert repo.calls == []


@pytest.mark.parametrize("method_name", _METHODS)
def test_every_report_method_passes_through_organization_and_filter(method_name):
    service, repo = _service()
    method = getattr(service, method_name)
    custom_filter = ReportFilter(warehouse_id=uuid.uuid4())

    method(custom_filter)

    assert len(repo.calls) == 1
    name, org_id, passed_filter = repo.calls[0]
    assert org_id == ORG_ID
    assert passed_filter is custom_filter


@pytest.mark.parametrize("method_name", _METHODS)
def test_every_report_method_defaults_to_empty_filter(method_name):
    service, repo = _service()
    method = getattr(service, method_name)

    method()

    assert len(repo.calls) == 1
    _, _, passed_filter = repo.calls[0]
    assert passed_filter == ReportFilter()
