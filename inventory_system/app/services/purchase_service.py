"""Application service for suppliers and the purchasing workflow (DRAFT ->
SUBMITTED -> APPROVED -> PARTIALLY_RECEIVED/RECEIVED, or -> CANCELLED). The
one seam the UI is allowed to call — status-machine legality, existence
checks, and permission enforcement live here; row locking and the
atomic inventory-ledger + audit-log write on receipt/return live in
PurchaseOrderRepository (SQL-only, needs a real transaction).

organization_id and the acting user always come from the current session,
never from a caller-supplied argument. "Purchase history" (a named
feature) isn't a separate method — it's search_purchase_orders, the same
method a live purchasing screen would call, just without a narrowing
filter.
"""
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from app.core.exceptions import (
    InvalidPurchaseOrderTransitionError,
    ProductNotFoundError,
    PurchaseOrderNotFoundError,
    PurchaseOrderValidationError,
    SupplierNotFoundError,
    WarehouseNotFoundError,
)
from app.domain.purchasing import (
    PurchaseOrderStatus,
    can_transition,
    validate_purchase_order_item,
    validate_supplier,
)
from app.repositories.interfaces import (
    ProductRepository,
    PurchaseOrderRepository,
    SupplierRepository,
    WarehouseRepository,
)
from app.schemas.transactions import TransactionListPage, TransactionListRow
from app.schemas.purchasing import (
    GoodsReceiptOut,
    PurchaseOrderCreate,
    PurchaseOrderFilter,
    PurchaseOrderOut,
    PurchaseOrderPage,
    PurchaseOrderUpdate,
    PurchaseReturnOut,
    ReceiveGoodsRequest,
    SupplierCreate,
    SupplierOut,
    SupplierUpdate,
)
from app.security.authorization import require_permission
from app.security.session import SessionManager


class PurchaseService:
    def __init__(self, suppliers: SupplierRepository, purchase_orders: PurchaseOrderRepository,
                products: ProductRepository, warehouses: WarehouseRepository,
                sessions: SessionManager):
        self._suppliers = suppliers
        self._purchase_orders = purchase_orders
        self._products = products
        self._warehouses = warehouses
        self._sessions = sessions

    def _organization_id(self) -> uuid.UUID:
        return self._sessions.current(now=datetime.now(timezone.utc)).organization_id

    def _current_user_id(self) -> uuid.UUID:
        return self._sessions.current(now=datetime.now(timezone.utc)).user_id

    def _require_purchase_order(self, purchase_order_id: uuid.UUID) -> PurchaseOrderOut:
        po = self._purchase_orders.get_by_id(self._organization_id(), purchase_order_id)
        if po is None:
            raise PurchaseOrderNotFoundError(purchase_order_id)
        return po

    # -- suppliers ------------------------------------------------------ #
    @require_permission("purchases.update")
    def create_supplier(self, data: SupplierCreate) -> SupplierOut:
        errors = validate_supplier(name=data.name, email=data.email, phone=data.phone)
        if errors:
            raise PurchaseOrderValidationError(errors)
        return self._suppliers.create(self._organization_id(), data)

    @require_permission("purchases.update")
    def update_supplier(self, supplier_id: uuid.UUID, data: SupplierUpdate) -> SupplierOut:
        if data.name is not None or data.email is not None or data.phone is not None:
            current = self._suppliers.get_by_id(self._organization_id(), supplier_id)
            if current is None:
                raise SupplierNotFoundError(supplier_id)
            errors = validate_supplier(
                name=data.name if data.name is not None else current.name,
                email=data.email if data.email is not None else current.email,
                phone=data.phone if data.phone is not None else current.phone)
            if errors:
                raise PurchaseOrderValidationError(errors)
        result = self._suppliers.update(self._organization_id(), supplier_id, data)
        if result is None:
            raise SupplierNotFoundError(supplier_id)
        return result

    @require_permission("purchases.view")
    def get_supplier(self, supplier_id: uuid.UUID) -> SupplierOut:
        result = self._suppliers.get_by_id(self._organization_id(), supplier_id)
        if result is None:
            raise SupplierNotFoundError(supplier_id)
        return result

    @require_permission("purchases.view")
    def list_suppliers(self) -> list[SupplierOut]:
        return self._suppliers.list_all(self._organization_id())

    # -- purchase orders -------------------------------------------------#
    def _validate_items(self, data) -> None:
        org_id = self._organization_id()
        if not data.items:
            raise PurchaseOrderValidationError(
                ["A purchase order needs at least one line item."])
        errors: list[str] = []
        for item in data.items:
            if self._products.get_by_id(org_id, item.product_id) is None:
                raise ProductNotFoundError(item.product_id)
            errors.extend(validate_purchase_order_item(
                quantity_ordered=item.quantity_ordered, unit_price=item.unit_price,
                tax_percent=item.tax_percent))
        if errors:
            raise PurchaseOrderValidationError(errors)

    @require_permission("purchases.create")
    def create_purchase_order(self, data: PurchaseOrderCreate) -> PurchaseOrderOut:
        org_id = self._organization_id()
        if self._suppliers.get_by_id(org_id, data.supplier_id) is None:
            raise SupplierNotFoundError(data.supplier_id)
        if self._warehouses.get_by_id(org_id, data.warehouse_id) is None:
            raise WarehouseNotFoundError(data.warehouse_id)
        self._validate_items(data)
        # Deliberately does not touch Inventory/InventoryTransaction at
        # all — a purchase order reserving/receiving nothing yet must not
        # move stock. See PurchaseOrderRepository.create and
        # tests/services/test_purchase_service.py.
        return self._purchase_orders.create(org_id, data, self._current_user_id())

    @require_permission("purchases.update")
    def update_purchase_order(self, purchase_order_id: uuid.UUID,
                              data: PurchaseOrderUpdate) -> PurchaseOrderOut:
        existing = self._require_purchase_order(purchase_order_id)
        if existing.status != PurchaseOrderStatus.DRAFT:
            raise PurchaseOrderValidationError(["Only draft purchase orders can be edited."])
        org_id = self._organization_id()
        if data.supplier_id is not None and self._suppliers.get_by_id(
                org_id, data.supplier_id) is None:
            raise SupplierNotFoundError(data.supplier_id)
        if data.warehouse_id is not None and self._warehouses.get_by_id(
                org_id, data.warehouse_id) is None:
            raise WarehouseNotFoundError(data.warehouse_id)
        if data.items is not None:
            self._validate_items(data)
        result = self._purchase_orders.update(org_id, purchase_order_id, data)
        if result is None:
            raise PurchaseOrderNotFoundError(purchase_order_id)
        return result

    @require_permission("purchases.view")
    def get_purchase_order(self, purchase_order_id: uuid.UUID) -> PurchaseOrderOut:
        return self._require_purchase_order(purchase_order_id)

    @require_permission("purchases.view")
    def search_purchase_orders(self, filter: PurchaseOrderFilter) -> PurchaseOrderPage:
        return self._purchase_orders.search(self._organization_id(), filter)

    @require_permission("purchases.view")
    def list_purchase_transactions(self, filter: PurchaseOrderFilter) -> TransactionListPage:
        """The Purchases register — same filter shape as
        search_purchase_orders, but flattened rows plus totals over the
        whole filtered set. See app.repositories.sql.transaction_list.
        """
        return self._purchase_orders.list_transactions(self._organization_id(), filter)

    @require_permission("purchases.view")
    def export_purchase_transactions(self,
                                     filter: PurchaseOrderFilter) -> list[TransactionListRow]:
        return self._purchase_orders.export_transactions(self._organization_id(), filter)

    def _transition_or_raise(self, purchase_order_id: uuid.UUID,
                             target: PurchaseOrderStatus) -> PurchaseOrderOut:
        existing = self._require_purchase_order(purchase_order_id)
        if not can_transition(existing.status, target):
            raise InvalidPurchaseOrderTransitionError(existing.status, target)
        return existing

    @require_permission("purchases.update")
    def submit_purchase_order(self, purchase_order_id: uuid.UUID) -> PurchaseOrderOut:
        self._transition_or_raise(purchase_order_id, PurchaseOrderStatus.SUBMITTED)
        result = self._purchase_orders.submit(self._organization_id(), purchase_order_id)
        if result is None:
            raise PurchaseOrderNotFoundError(purchase_order_id)
        return result

    @require_permission("purchases.approve")
    def approve_purchase_order(self, purchase_order_id: uuid.UUID) -> PurchaseOrderOut:
        self._transition_or_raise(purchase_order_id, PurchaseOrderStatus.APPROVED)
        result = self._purchase_orders.approve(self._organization_id(), purchase_order_id,
                                               self._current_user_id())
        if result is None:
            raise PurchaseOrderNotFoundError(purchase_order_id)
        return result

    @require_permission("purchases.cancel")
    def cancel_purchase_order(self, purchase_order_id: uuid.UUID) -> PurchaseOrderOut:
        # No separate "already has received goods" check needed here: the
        # state machine (app.domain.purchasing.ALLOWED_TRANSITIONS) already
        # excludes CANCELLED as a target from PARTIALLY_RECEIVED/RECEIVED,
        # and receive_goods always advances status to one of those the
        # moment any item's quantity_received becomes > 0 — so an order
        # with anything received is never still sitting in a
        # CANCELLED-reachable status. Once something's been received, a
        # purchase return is the correct tool, not cancellation.
        self._transition_or_raise(purchase_order_id, PurchaseOrderStatus.CANCELLED)
        result = self._purchase_orders.cancel(self._organization_id(), purchase_order_id,
                                              self._current_user_id())
        if result is None:
            raise PurchaseOrderNotFoundError(purchase_order_id)
        return result

    @require_permission("purchases.receive")
    def receive_goods(self, data: ReceiveGoodsRequest) -> GoodsReceiptOut:
        self._require_purchase_order(data.purchase_order_id)
        if not data.lines:
            raise PurchaseOrderValidationError(
                ["At least one line item is required to receive goods."])
        return self._purchase_orders.receive_goods(
            self._organization_id(), data.purchase_order_id, data.lines,
            self._current_user_id(), notes=data.notes)

    @require_permission("purchases.return")
    def record_return(self, purchase_order_id: uuid.UUID, purchase_order_item_id: uuid.UUID,
                      quantity: Decimal, reason: str) -> PurchaseReturnOut:
        self._require_purchase_order(purchase_order_id)
        errors = []
        if quantity <= 0:
            errors.append("Quantity must be greater than zero.")
        if not reason.strip():
            errors.append("A reason is required for a purchase return.")
        if errors:
            raise PurchaseOrderValidationError(errors)
        return self._purchase_orders.record_return(
            self._organization_id(), purchase_order_id, purchase_order_item_id, quantity,
            reason.strip(), self._current_user_id())
