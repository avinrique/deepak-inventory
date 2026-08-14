"""Application service for the Dashboard and the nine exportable reports —
a thin permission-enforcing wrapper. All the actual aggregation lives in
ReportingRepository (SQL-only: dashboards/reports are a read-only view over
data that already lives in Postgres, there's no Excel equivalent to keep in
sync). organization_id always comes from the current session.
"""
import uuid
from datetime import datetime, timezone

from app.repositories.interfaces import ReportingRepository
from app.schemas.reporting import DashboardMetrics, ReportFilter, ReportResult
from app.security.authorization import require_permission
from app.security.session import SessionManager


class ReportingService:
    def __init__(self, reporting: ReportingRepository, sessions: SessionManager):
        self._reporting = reporting
        self._sessions = sessions

    def _organization_id(self) -> uuid.UUID:
        return self._sessions.current(now=datetime.now(timezone.utc)).organization_id

    @require_permission("reports.view")
    def get_dashboard_metrics(self, filter: ReportFilter | None = None) -> DashboardMetrics:
        return self._reporting.get_dashboard_metrics(self._organization_id(),
                                                      filter or ReportFilter())

    @require_permission("reports.view")
    def stock_report(self, filter: ReportFilter | None = None) -> ReportResult:
        return self._reporting.stock_report(self._organization_id(), filter or ReportFilter())

    @require_permission("reports.view")
    def sales_report(self, filter: ReportFilter | None = None) -> ReportResult:
        return self._reporting.sales_report(self._organization_id(), filter or ReportFilter())

    @require_permission("reports.view")
    def purchase_report(self, filter: ReportFilter | None = None) -> ReportResult:
        return self._reporting.purchase_report(self._organization_id(), filter or ReportFilter())

    @require_permission("reports.view")
    def profit_report(self, filter: ReportFilter | None = None) -> ReportResult:
        return self._reporting.profit_report(self._organization_id(), filter or ReportFilter())

    @require_permission("reports.view")
    def low_stock_report(self, filter: ReportFilter | None = None) -> ReportResult:
        return self._reporting.low_stock_report(self._organization_id(), filter or ReportFilter())

    @require_permission("reports.view")
    def supplier_report(self, filter: ReportFilter | None = None) -> ReportResult:
        return self._reporting.supplier_report(self._organization_id(), filter or ReportFilter())

    @require_permission("reports.view")
    def customer_report(self, filter: ReportFilter | None = None) -> ReportResult:
        return self._reporting.customer_report(self._organization_id(), filter or ReportFilter())

    @require_permission("reports.view")
    def product_movement_report(self, filter: ReportFilter | None = None) -> ReportResult:
        return self._reporting.product_movement_report(self._organization_id(),
                                                        filter or ReportFilter())

    @require_permission("reports.view")
    def inventory_valuation_report(self, filter: ReportFilter | None = None) -> ReportResult:
        return self._reporting.inventory_valuation_report(self._organization_id(),
                                                           filter or ReportFilter())
