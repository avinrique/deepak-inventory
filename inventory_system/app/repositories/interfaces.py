"""Repository contracts. Services depend on these Protocols, never on a
concrete Excel or SQL implementation — that's what makes the backend
swappable per docs/architecture.md's migration strategy.
"""
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from app.domain.product import ProductStatus
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
from app.schemas.party import PartyOut
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
from app.schemas.user import MembershipOut, UserCredentials, UserOut


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

    def get_role_permissions(self, role_id: uuid.UUID) -> frozenset[str]: ...

    def create_user(self, email: str, full_name: str, hashed_password: str,
                    organization_id: uuid.UUID, role_id: uuid.UUID,
                    is_default: bool = True) -> UserOut: ...

    def set_active(self, user_id: uuid.UUID, is_active: bool) -> None: ...

    def update_password_hash(self, user_id: uuid.UUID, new_hash: str,
                             must_change_password: bool = False) -> None: ...

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
