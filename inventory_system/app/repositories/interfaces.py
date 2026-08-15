"""Repository contracts. Services depend on these Protocols, never on a
concrete Excel or SQL implementation — that's what makes the backend
swappable per docs/architecture.md's migration strategy.
"""
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from app.domain.backup import BackupStatus
from app.domain.product import ProductStatus
from app.schemas.backup import BackupOut
from app.schemas.bill import BillCreate, BillOut
from app.schemas.inventory import (
    InventoryLevel,
    InventoryTransactionOut,
    InventoryTransactionPage,
    TransactionFilter,
    WarehouseCreate,
    WarehouseOut,
    WarehouseUpdate,
)
from app.schemas.organization import OrganizationOut, OrganizationUpdate
from app.schemas.party import PartyOut
from app.schemas.reporting import DashboardMetrics, ReportFilter, ReportResult
from app.schemas.sales import (
    CustomerCreate,
    CustomerOut,
    CustomerUpdate,
    InvoiceDocumentData,
    InvoiceOut,
    PaymentOut,
    PaymentRequest,
    SalesOrderCreate,
    SalesOrderFilter,
    SalesOrderOut,
    SalesOrderPage,
    SalesOrderUpdate,
    SalesReturnOut,
)
from app.schemas.purchasing import (
    GoodsReceiptLineInput,
    GoodsReceiptOut,
    PurchaseOrderCreate,
    PurchaseOrderFilter,
    PurchaseOrderOut,
    PurchaseOrderPage,
    PurchaseOrderUpdate,
    PurchaseReturnOut,
    SupplierCreate,
    SupplierOut,
    SupplierUpdate,
)
from app.schemas.product import (
    BrandCreate,
    BrandOut,
    CategoryCreate,
    CategoryOut,
    ProductCreate,
    ProductFilter,
    ProductOut,
    ProductPage,
    ProductUpdate,
    UnitCreate,
    UnitOut,
)
from app.schemas.stock import StockLevel
from app.schemas.user import (
    MembershipOut,
    RoleOut,
    UserCredentials,
    UserOut,
    UserSummaryOut,
    UserUpdate,
)


class BillRepository(Protocol):
    def append(self, bill: BillCreate, subtotal: Decimal, vat_amount: Decimal,
              total: Decimal) -> None: ...

    def exists(self, bill_no: str) -> bool: ...

    def list_all(self) -> list[BillOut]: ...


class StockRepository(Protocol):
    def on_hand(self, product: str) -> Decimal: ...

    def adjust(self, product: str, qty: Decimal, add: bool) -> Decimal: ...

    def product_names(self) -> list[str]: ...

    def list_all(self) -> list[StockLevel]: ...


class PartyRepository(Protocol):
    def upsert_totals(self, pan: str, name: str, address: str,
                      total: Decimal, is_sale: bool) -> None: ...

    def list_all(self) -> list[PartyOut]: ...


class UserRepository(Protocol):
    """PostgreSQL-only — there is no Excel equivalent (identity/auth is a
    capability the legacy app never had), so unlike Bill/Stock/Party this
    has no excel/ implementation to swap with.
    """
    def get_by_id(self, user_id: uuid.UUID) -> UserOut | None: ...

    def get_credentials_by_email(self, email: str) -> UserCredentials | None: ...

    def get_credentials_by_id(self, user_id: uuid.UUID) -> UserCredentials | None: ...

    def get_membership(self, user_id: uuid.UUID,
                       organization_id: uuid.UUID) -> MembershipOut | None: ...

    def list_memberships(self, user_id: uuid.UUID) -> list[MembershipOut]: ...

    def list_users(self, organization_id: uuid.UUID) -> list[UserSummaryOut]: ...

    def get_user(self, user_id: uuid.UUID,
                organization_id: uuid.UUID) -> UserSummaryOut | None: ...

    def list_roles(self) -> list[RoleOut]: ...

    def role_exists(self, role_id: uuid.UUID) -> bool: ...

    def update_membership_role(self, user_id: uuid.UUID, organization_id: uuid.UUID,
                              role_id: uuid.UUID) -> MembershipOut | None: ...

    def get_role_permissions(self, role_id: uuid.UUID) -> frozenset[str]: ...

    def email_exists(self, email: str, exclude_user_id: uuid.UUID | None = None) -> bool: ...

    def username_exists(self, username: str,
                        exclude_user_id: uuid.UUID | None = None) -> bool: ...

    def create_user(self, email: str, full_name: str, hashed_password: str,
                    organization_id: uuid.UUID, role_id: uuid.UUID, username: str,
                    phone: str | None = None, is_default: bool = True) -> UserOut: ...

    def update_profile(self, user_id: uuid.UUID, organization_id: uuid.UUID,
                       data: UserUpdate) -> UserOut | None: ...

    def set_active(self, user_id: uuid.UUID, organization_id: uuid.UUID,
                   is_active: bool) -> bool: ...

    def update_password_hash(self, user_id: uuid.UUID, organization_id: uuid.UUID,
                             new_hash: str, must_change_password: bool = False) -> bool: ...

    def clear_must_change_password(self, user_id: uuid.UUID) -> None: ...

    def record_login(self, user_id: uuid.UUID, when: datetime) -> None: ...


class CategoryRepository(Protocol):
    """PostgreSQL-only, same as UserRepository — no Excel equivalent."""
    def create(self, organization_id: uuid.UUID, data: CategoryCreate) -> CategoryOut: ...

    def update(self, organization_id: uuid.UUID, category_id: uuid.UUID,
              data: CategoryCreate) -> CategoryOut | None: ...

    def delete(self, organization_id: uuid.UUID, category_id: uuid.UUID) -> None: ...

    def list_all(self, organization_id: uuid.UUID) -> list[CategoryOut]: ...

    def get_by_id(self, organization_id: uuid.UUID,
                 category_id: uuid.UUID) -> CategoryOut | None: ...


class BrandRepository(Protocol):
    def create(self, organization_id: uuid.UUID, data: BrandCreate) -> BrandOut: ...

    def update(self, organization_id: uuid.UUID, brand_id: uuid.UUID,
              data: BrandCreate) -> BrandOut | None: ...

    def delete(self, organization_id: uuid.UUID, brand_id: uuid.UUID) -> None: ...

    def list_all(self, organization_id: uuid.UUID) -> list[BrandOut]: ...

    def get_by_id(self, organization_id: uuid.UUID, brand_id: uuid.UUID) -> BrandOut | None: ...


class UnitRepository(Protocol):
    def create(self, organization_id: uuid.UUID, data: UnitCreate) -> UnitOut: ...

    def update(self, organization_id: uuid.UUID, unit_id: uuid.UUID,
              data: UnitCreate) -> UnitOut | None: ...

    def delete(self, organization_id: uuid.UUID, unit_id: uuid.UUID) -> None: ...

    def list_all(self, organization_id: uuid.UUID) -> list[UnitOut]: ...

    def get_by_id(self, organization_id: uuid.UUID, unit_id: uuid.UUID) -> UnitOut | None: ...


class ProductRepository(Protocol):
    def create(self, organization_id: uuid.UUID, data: ProductCreate) -> ProductOut: ...

    def update(self, organization_id: uuid.UUID, product_id: uuid.UUID,
              data: ProductUpdate) -> ProductOut | None: ...

    def get_by_id(self, organization_id: uuid.UUID,
                 product_id: uuid.UUID) -> ProductOut | None: ...

    def sku_exists(self, organization_id: uuid.UUID, sku: str,
                   exclude_id: uuid.UUID | None = None) -> bool: ...

    def barcode_exists(self, organization_id: uuid.UUID, barcode: str,
                       exclude_id: uuid.UUID | None = None) -> bool: ...

    def set_status(self, organization_id: uuid.UUID, product_id: uuid.UUID,
                   status: ProductStatus) -> None: ...

    def search(self, organization_id: uuid.UUID, filter: ProductFilter) -> ProductPage: ...


class WarehouseRepository(Protocol):
    def create(self, organization_id: uuid.UUID, data: WarehouseCreate) -> WarehouseOut: ...

    def update(self, organization_id: uuid.UUID, warehouse_id: uuid.UUID,
              data: WarehouseUpdate) -> WarehouseOut | None: ...

    def get_by_id(self, organization_id: uuid.UUID,
                 warehouse_id: uuid.UUID) -> WarehouseOut | None: ...

    def code_exists(self, organization_id: uuid.UUID, code: str,
                    exclude_id: uuid.UUID | None = None) -> bool: ...

    def list_all(self, organization_id: uuid.UUID) -> list[WarehouseOut]: ...


class InventoryRepository(Protocol):
    """Every method here runs its work inside one database transaction
    (see app.repositories.sql.inventory_repository) — lock the relevant
    Inventory row(s), validate, update the snapshot, append an immutable
    InventoryTransaction row, commit or roll back together. quantity
    arguments are always unsigned (> 0); direction is implied by the
    method, not by the sign the caller passes in.
    """

    def stock_in(self, organization_id: uuid.UUID, product_id: uuid.UUID,
                warehouse_id: uuid.UUID, quantity: Decimal, performed_by: uuid.UUID,
                notes: str | None = None) -> InventoryTransactionOut: ...

    def stock_out(self, organization_id: uuid.UUID, product_id: uuid.UUID,
                 warehouse_id: uuid.UUID, quantity: Decimal, performed_by: uuid.UUID,
                 notes: str | None = None) -> InventoryTransactionOut: ...

    def mark_damaged(self, organization_id: uuid.UUID, product_id: uuid.UUID,
                     warehouse_id: uuid.UUID, quantity: Decimal, performed_by: uuid.UUID,
                     notes: str | None = None) -> InventoryTransactionOut: ...

    def record_return(self, organization_id: uuid.UUID, product_id: uuid.UUID,
                      warehouse_id: uuid.UUID, quantity: Decimal, performed_by: uuid.UUID,
                      to_stock: bool, notes: str | None = None) -> InventoryTransactionOut: ...

    def adjust(self, organization_id: uuid.UUID, product_id: uuid.UUID,
              warehouse_id: uuid.UUID, quantity_change: Decimal, reason: str,
              performed_by: uuid.UUID) -> InventoryTransactionOut: ...

    def transfer(self, organization_id: uuid.UUID, product_id: uuid.UUID,
                from_warehouse_id: uuid.UUID, to_warehouse_id: uuid.UUID, quantity: Decimal,
                performed_by: uuid.UUID, notes: str | None = None
                ) -> tuple[InventoryTransactionOut, InventoryTransactionOut]: ...

    def reserve(self, organization_id: uuid.UUID, product_id: uuid.UUID,
               warehouse_id: uuid.UUID, quantity: Decimal, performed_by: uuid.UUID,
               notes: str | None = None) -> InventoryTransactionOut: ...

    def release_reservation(self, organization_id: uuid.UUID, product_id: uuid.UUID,
                            warehouse_id: uuid.UUID, quantity: Decimal, performed_by: uuid.UUID,
                            notes: str | None = None) -> InventoryTransactionOut: ...

    def get_level(self, organization_id: uuid.UUID, product_id: uuid.UUID,
                 warehouse_id: uuid.UUID) -> InventoryLevel: ...

    def list_levels_for_product(self, organization_id: uuid.UUID,
                                product_id: uuid.UUID) -> list[InventoryLevel]: ...

    def list_transactions(self, organization_id: uuid.UUID,
                          filter: TransactionFilter) -> InventoryTransactionPage: ...


class SupplierRepository(Protocol):
    def create(self, organization_id: uuid.UUID, data: SupplierCreate) -> SupplierOut: ...

    def update(self, organization_id: uuid.UUID, supplier_id: uuid.UUID,
              data: SupplierUpdate) -> SupplierOut | None: ...

    def get_by_id(self, organization_id: uuid.UUID,
                 supplier_id: uuid.UUID) -> SupplierOut | None: ...

    def list_all(self, organization_id: uuid.UUID) -> list[SupplierOut]: ...


class PurchaseOrderRepository(Protocol):
    """Every state-changing method runs inside one database transaction —
    see app.repositories.sql.purchase_repository. receive_goods and
    record_return additionally update the inventory ledger and append an
    audit log entry in that same transaction, by reusing
    app.repositories.sql.inventory_repository's locked ``_apply`` helper
    directly rather than calling InventoryRepository (which would open its
    own, separate transaction).
    """

    def create(self, organization_id: uuid.UUID, data: PurchaseOrderCreate,
              created_by: uuid.UUID) -> PurchaseOrderOut: ...

    def update(self, organization_id: uuid.UUID, purchase_order_id: uuid.UUID,
              data: PurchaseOrderUpdate) -> PurchaseOrderOut | None: ...

    def get_by_id(self, organization_id: uuid.UUID,
                 purchase_order_id: uuid.UUID) -> PurchaseOrderOut | None: ...

    def search(self, organization_id: uuid.UUID,
              filter: PurchaseOrderFilter) -> PurchaseOrderPage: ...

    def submit(self, organization_id: uuid.UUID,
              purchase_order_id: uuid.UUID) -> PurchaseOrderOut | None: ...

    def approve(self, organization_id: uuid.UUID, purchase_order_id: uuid.UUID,
              approved_by: uuid.UUID) -> PurchaseOrderOut | None: ...

    def cancel(self, organization_id: uuid.UUID, purchase_order_id: uuid.UUID,
              cancelled_by: uuid.UUID) -> PurchaseOrderOut | None: ...

    def receive_goods(self, organization_id: uuid.UUID, purchase_order_id: uuid.UUID,
                      lines: list[GoodsReceiptLineInput], received_by: uuid.UUID,
                      notes: str | None = None) -> GoodsReceiptOut: ...

    def record_return(self, organization_id: uuid.UUID, purchase_order_id: uuid.UUID,
                      purchase_order_item_id: uuid.UUID, quantity: Decimal, reason: str,
                      returned_by: uuid.UUID) -> PurchaseReturnOut: ...


class CustomerRepository(Protocol):
    def create(self, organization_id: uuid.UUID, data: CustomerCreate) -> CustomerOut: ...

    def update(self, organization_id: uuid.UUID, customer_id: uuid.UUID,
              data: CustomerUpdate) -> CustomerOut | None: ...

    def get_by_id(self, organization_id: uuid.UUID,
                 customer_id: uuid.UUID) -> CustomerOut | None: ...

    def list_all(self, organization_id: uuid.UUID) -> list[CustomerOut]: ...


class SalesOrderRepository(Protocol):
    """Every state-changing method runs inside one database transaction —
    see app.repositories.sql.sales_repository. fulfill_sale and
    record_return update the inventory ledger and append an audit log
    entry in that same transaction, reusing
    app.repositories.sql.inventory_repository's locked ``_apply`` helper
    directly, the same pattern PurchaseOrderRepository uses for goods
    receipt. generate_invoice and record_payment are sales-specific: no
    purchasing equivalent exists.
    """

    def create(self, organization_id: uuid.UUID, data: SalesOrderCreate,
              created_by: uuid.UUID) -> SalesOrderOut: ...

    def update(self, organization_id: uuid.UUID, sales_order_id: uuid.UUID,
              data: SalesOrderUpdate) -> SalesOrderOut | None: ...

    def get_by_id(self, organization_id: uuid.UUID,
                 sales_order_id: uuid.UUID) -> SalesOrderOut | None: ...

    def search(self, organization_id: uuid.UUID,
              filter: SalesOrderFilter) -> SalesOrderPage: ...

    def confirm(self, organization_id: uuid.UUID, sales_order_id: uuid.UUID,
              confirmed_by: uuid.UUID) -> SalesOrderOut | None: ...

    def cancel(self, organization_id: uuid.UUID, sales_order_id: uuid.UUID,
              cancelled_by: uuid.UUID) -> SalesOrderOut | None: ...

    def fulfill_sale(self, organization_id: uuid.UUID, sales_order_id: uuid.UUID,
                    fulfilled_by: uuid.UUID) -> SalesOrderOut: ...

    def generate_invoice(self, organization_id: uuid.UUID, sales_order_id: uuid.UUID,
                        generated_by: uuid.UUID) -> InvoiceOut: ...

    def get_invoice(self, organization_id: uuid.UUID,
                   invoice_id: uuid.UUID) -> InvoiceOut | None: ...

    def get_invoice_by_sales_order(self, organization_id: uuid.UUID,
                                   sales_order_id: uuid.UUID) -> InvoiceOut | None: ...

    def get_invoice_document(self, organization_id: uuid.UUID,
                            invoice_id: uuid.UUID) -> InvoiceDocumentData | None: ...

    def record_payment(self, organization_id: uuid.UUID, data: PaymentRequest,
                      recorded_by: uuid.UUID) -> PaymentOut: ...

    def record_return(self, organization_id: uuid.UUID, sales_order_id: uuid.UUID,
                      sales_order_item_id: uuid.UUID, quantity: Decimal, reason: str,
                      returned_by: uuid.UUID) -> SalesReturnOut: ...


class ReportingRepository(Protocol):
    """Read-only, and always SQL-aggregated — no method here loads a whole
    table's rows into Python to sum/count/group them in application code
    (see app.repositories.sql.reporting_repository's module docstring).
    """

    def get_dashboard_metrics(self, organization_id: uuid.UUID,
                              filter: ReportFilter) -> DashboardMetrics: ...

    def stock_report(self, organization_id: uuid.UUID, filter: ReportFilter) -> ReportResult: ...

    def sales_report(self, organization_id: uuid.UUID, filter: ReportFilter) -> ReportResult: ...

    def purchase_report(self, organization_id: uuid.UUID,
                        filter: ReportFilter) -> ReportResult: ...

    def profit_report(self, organization_id: uuid.UUID,
                      filter: ReportFilter) -> ReportResult: ...

    def low_stock_report(self, organization_id: uuid.UUID,
                         filter: ReportFilter) -> ReportResult: ...

    def supplier_report(self, organization_id: uuid.UUID,
                        filter: ReportFilter) -> ReportResult: ...

    def customer_report(self, organization_id: uuid.UUID,
                        filter: ReportFilter) -> ReportResult: ...

    def product_movement_report(self, organization_id: uuid.UUID,
                                filter: ReportFilter) -> ReportResult: ...

    def inventory_valuation_report(self, organization_id: uuid.UUID,
                                   filter: ReportFilter) -> ReportResult: ...


class OrganizationRepository(Protocol):
    def get_by_id(self, organization_id: uuid.UUID) -> OrganizationOut | None: ...

    def update(self, organization_id: uuid.UUID,
              data: OrganizationUpdate) -> OrganizationOut | None: ...

    def get_logo(self, organization_id: uuid.UUID) -> tuple[bytes, str | None] | None: ...


class AuditLogRepository(Protocol):
    """Append-only — there is deliberately no read/list method here yet;
    nothing in this codebase consumes audit log entries yet (see
    audit_logs.view in app.security.permissions, granted but not wired to
    anything). Writing is what Purchasing's goods-receipt flow and Sales'
    fulfillment/return/payment flows need.
    """

    def record(self, *, organization_id: uuid.UUID | None, user_id: uuid.UUID | None,
              actor_email: str | None, organization_name: str | None, action: str,
              entity_type: str | None = None, entity_id: uuid.UUID | None = None,
              changes: dict | None = None) -> None: ...


class BackupRepository(Protocol):
    """Backup metadata is deliberately not organization-scoped — see
    app.models.backup.DatabaseBackup's docstring: a backup covers the
    whole physical database, not one tenant's slice of it.
    """

    def record(self, *, organization_id: uuid.UUID | None, created_by: uuid.UUID | None,
              filename: str, file_path: str, file_size_bytes: int | None,
              checksum_sha256: str | None, status: BackupStatus, verified: bool,
              error_message: str | None, completed_at: datetime | None) -> BackupOut: ...

    def update_verification(self, backup_id: uuid.UUID, *,
                            verified: bool) -> BackupOut | None: ...

    def get_by_id(self, backup_id: uuid.UUID) -> BackupOut | None: ...

    def list_all(self) -> list[BackupOut]: ...

    def delete(self, backup_id: uuid.UUID) -> None: ...
