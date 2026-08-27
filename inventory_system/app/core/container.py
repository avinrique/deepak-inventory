"""Composition root: builds Services with their repositories.

Services and the UI only ever see the Protocols in
app.repositories.interfaces, never a concrete implementation — this is the
one place the concrete SQL classes are named.

Everything talks to `settings.database_url`. The old INVENTORY_BACKEND flag
that chose between an Excel backend and PostgreSQL is gone: the Excel path
wrapped the legacy Tkinter app's storage.py, which lives outside this
project and cannot be packaged, and the only service it still fed
(StockService) had already been superseded by InventoryService's warehouse
ledger and was no longer read by any page.
"""
from datetime import timedelta

from app.config.settings import settings
from app.repositories.sql.audit_log_repository import SqlAuditLogRepository
from app.repositories.sql.backup_repository import SqlBackupRepository
from app.repositories.sql.brand_repository import SqlBrandRepository
from app.repositories.sql.category_repository import SqlCategoryRepository
from app.repositories.sql.customer_repository import SqlCustomerRepository
from app.repositories.sql.inventory_repository import SqlInventoryRepository
from app.repositories.sql.organization_repository import SqlOrganizationRepository
from app.repositories.sql.product_repository import SqlProductRepository
from app.repositories.sql.purchase_repository import SqlPurchaseOrderRepository
from app.repositories.sql.reporting_repository import SqlReportingRepository
from app.repositories.sql.sales_repository import SqlSalesOrderRepository
from app.repositories.sql.supplier_repository import SqlSupplierRepository
from app.repositories.sql.unit_repository import SqlUnitRepository
from app.repositories.sql.user_repository import SqlUserRepository
from app.repositories.sql.warehouse_repository import SqlWarehouseRepository
from app.security.session import SessionManager
from app.services.auth_service import AuthService
from app.services.backup_service import BackupService
from app.services.catalog_service import CatalogService
from app.services.inventory_service import InventoryService
from app.services.organization_service import OrganizationService
from app.services.product_service import ProductService
from app.services.purchase_service import PurchaseService
from app.services.reporting_service import ReportingService
from app.services.sales_service import SalesService
from app.services.user_service import UserService


class Container:
    """Manual dependency-injection container — no framework, just factories.

    Built once in app.main; Services (and, through them, the UI) receive
    their dependencies via this container rather than constructing
    repositories themselves.
    """

    def __init__(self):
        self.user_repo = SqlUserRepository()
        self.category_repo = SqlCategoryRepository()
        self.brand_repo = SqlBrandRepository()
        self.unit_repo = SqlUnitRepository()
        self.product_repo = SqlProductRepository()
        self.warehouse_repo = SqlWarehouseRepository()
        self.inventory_repo = SqlInventoryRepository()
        self.supplier_repo = SqlSupplierRepository()
        self.purchase_order_repo = SqlPurchaseOrderRepository()
        self.customer_repo = SqlCustomerRepository()
        self.sales_order_repo = SqlSalesOrderRepository()
        self.reporting_repo = SqlReportingRepository()
        self.organization_repo = SqlOrganizationRepository()
        self.audit_log_repo = SqlAuditLogRepository()
        self.backup_repo = SqlBackupRepository()
        self.sessions = SessionManager(
            idle_timeout=timedelta(minutes=settings.session_idle_timeout_minutes))

    def auth_service(self) -> AuthService:
        return AuthService(self.user_repo, self.sessions, self.audit_log_repo,
                           self.organization_repo,
                           lockout_threshold=settings.login_lockout_threshold,
                           lockout_window=timedelta(
                               minutes=settings.login_lockout_window_minutes),
                           lockout_duration=timedelta(
                               minutes=settings.login_lockout_duration_minutes))

    def user_service(self) -> UserService:
        return UserService(self.user_repo, self.sessions, self.audit_log_repo,
                           self.organization_repo)

    def product_service(self) -> ProductService:
        return ProductService(self.product_repo, self.sessions, self.audit_log_repo,
                              self.warehouse_repo, self.unit_repo)

    def catalog_service(self) -> CatalogService:
        return CatalogService(self.category_repo, self.brand_repo, self.unit_repo,
                              self.sessions)

    def inventory_service(self) -> InventoryService:
        return InventoryService(self.warehouse_repo, self.inventory_repo, self.product_repo,
                                self.sessions)

    def purchase_service(self) -> PurchaseService:
        return PurchaseService(self.supplier_repo, self.purchase_order_repo, self.product_repo,
                               self.warehouse_repo, self.sessions)

    def sales_service(self) -> SalesService:
        return SalesService(self.customer_repo, self.sales_order_repo, self.product_repo,
                            self.warehouse_repo, self.sessions, self.audit_log_repo)

    def reporting_service(self) -> ReportingService:
        return ReportingService(self.reporting_repo, self.sessions)

    def organization_service(self) -> OrganizationService:
        return OrganizationService(self.organization_repo, self.sessions, self.audit_log_repo,
                                   self.warehouse_repo)

    def backup_service(self) -> BackupService:
        return BackupService(self.backup_repo, self.sessions, self.audit_log_repo,
                             self.organization_repo)
