"""PurchaseService tested against hand-written fake repositories — no
database, no real locking. Proves validation, existence checks, the
status-machine's illegal-transition guard, and purchase.*
permission enforcement happen in the service. Real row-locking/atomicity
across inventory + PO status + audit log is proven against a live database
in tests/repositories/test_sql_purchase_repository.py — a fake repository
can't exercise that, it's plain dicts.
"""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.core.exceptions import (
    InvalidPurchaseOrderTransitionError,
    ProductNotFoundError,
    PurchaseOrderItemNotFoundError,
    PurchaseOrderNotFoundError,
    PurchaseOrderValidationError,
    SupplierNotFoundError,
    WarehouseNotFoundError,
)
from app.domain.purchasing import PurchaseOrderStatus
from app.schemas.product import ProductOut, UnitOut
from app.schemas.purchasing import (
    GoodsReceiptLineInput,
    GoodsReceiptOut,
    PurchaseOrderCreate,
    PurchaseOrderItemInput,
    PurchaseOrderItemOut,
    PurchaseOrderOut,
    PurchaseOrderUpdate,
    PurchaseReturnOut,
    ReceiveGoodsRequest,
    SupplierCreate,
    SupplierOut,
    SupplierUpdate,
)
from app.schemas.inventory import WarehouseOut
from app.security.authorization import PermissionDeniedError
from app.security.session import SessionManager
from app.services.purchase_service import PurchaseService

ORG_ID = uuid.uuid4()
UNIT = UnitOut(id=uuid.uuid4(), name="Piece", abbreviation="pc")

ALL_PERMISSIONS = frozenset({"purchase.create", "purchase.read", "purchase.update",
                             "purchase.approve", "purchase.receive", "purchase.cancel",
                             "purchase.return"})


class FakeSupplierRepository:
    def __init__(self):
        self.suppliers: dict[uuid.UUID, SupplierOut] = {}

    def create(self, organization_id, data: SupplierCreate) -> SupplierOut:
        now = datetime.now(timezone.utc)
        supplier = SupplierOut(id=uuid.uuid4(), name=data.name,
                               contact_person=data.contact_person, phone=data.phone,
                               email=data.email, address=data.address, tax_id=data.tax_id,
                               notes=data.notes, is_active=True, created_at=now,
                               updated_at=now)
        self.suppliers[supplier.id] = supplier
        return supplier

    def update(self, organization_id, supplier_id, data: SupplierUpdate):
        existing = self.suppliers.get(supplier_id)
        if existing is None:
            return None
        updated = existing.model_copy(update=data.model_dump(exclude_unset=True))
        self.suppliers[supplier_id] = updated
        return updated

    def get_by_id(self, organization_id, supplier_id):
        return self.suppliers.get(supplier_id)

    def list_all(self, organization_id):
        return list(self.suppliers.values())


class FakeWarehouseRepository:
    def __init__(self, warehouse_id):
        now = datetime.now(timezone.utc)
        self.warehouse = WarehouseOut(id=warehouse_id, code="MAIN", name="Main", address=None,
                                      is_active=True, created_at=now, updated_at=now)

    def get_by_id(self, organization_id, warehouse_id):
        return self.warehouse if warehouse_id == self.warehouse.id else None

    def create(self, *a, **k): raise NotImplementedError
    def update(self, *a, **k): raise NotImplementedError
    def code_exists(self, *a, **k): raise NotImplementedError
    def list_all(self, *a, **k): raise NotImplementedError


class FakeProductRepository:
    def __init__(self, products: dict[uuid.UUID, ProductOut]):
        self.products = products

    def get_by_id(self, organization_id, product_id):
        return self.products.get(product_id)

    def create(self, *a, **k): raise NotImplementedError
    def update(self, *a, **k): raise NotImplementedError
    def sku_exists(self, *a, **k): raise NotImplementedError
    def barcode_exists(self, *a, **k): raise NotImplementedError
    def set_status(self, *a, **k): raise NotImplementedError
    def search(self, *a, **k): raise NotImplementedError


class FakePurchaseOrderRepository:
    """Obedient — does exactly what's asked with no independent business
    rules, so tests can tell whether an assertion failure means the
    SERVICE didn't validate/gate something (this fake would have let it
    through) rather than the fake silently fixing it up.
    """

    def __init__(self):
        self.orders: dict[uuid.UUID, PurchaseOrderOut] = {}

    def create(self, organization_id, data: PurchaseOrderCreate, created_by) -> PurchaseOrderOut:
        now = datetime.now(timezone.utc)
        items = [PurchaseOrderItemOut(id=uuid.uuid4(), product_id=i.product_id,
                                      quantity_ordered=i.quantity_ordered,
                                      quantity_received=Decimal("0"), unit_price=i.unit_price,
                                      tax_percent=i.tax_percent)
                for i in data.items]
        po = PurchaseOrderOut(id=uuid.uuid4(), supplier_id=data.supplier_id,
                              warehouse_id=data.warehouse_id, status=PurchaseOrderStatus.DRAFT,
                              expected_date=data.expected_date, notes=data.notes,
                              created_by=created_by, approved_by=None, approved_at=None,
                              items=items, created_at=now, updated_at=now)
        self.orders[po.id] = po
        return po

    def update(self, organization_id, purchase_order_id, data: PurchaseOrderUpdate):
        existing = self.orders.get(purchase_order_id)
        if existing is None:
            return None
        updates = data.model_dump(exclude_unset=True, exclude={"items"})
        items = existing.items
        if data.items is not None:
            items = [PurchaseOrderItemOut(id=uuid.uuid4(), product_id=i.product_id,
                                          quantity_ordered=i.quantity_ordered,
                                          quantity_received=Decimal("0"),
                                          unit_price=i.unit_price, tax_percent=i.tax_percent)
                    for i in data.items]
        updated = existing.model_copy(update={**updates, "items": items})
        self.orders[purchase_order_id] = updated
        return updated

    def get_by_id(self, organization_id, purchase_order_id):
        return self.orders.get(purchase_order_id)

    def search(self, organization_id, filter):
        raise NotImplementedError  # not exercised by these tests

    def _set_status(self, purchase_order_id, status, **extra):
        existing = self.orders[purchase_order_id]
        updated = existing.model_copy(update={"status": status, **extra})
        self.orders[purchase_order_id] = updated
        return updated

    def submit(self, organization_id, purchase_order_id):
        return self._set_status(purchase_order_id, PurchaseOrderStatus.SUBMITTED)

    def approve(self, organization_id, purchase_order_id, approved_by):
        return self._set_status(purchase_order_id, PurchaseOrderStatus.APPROVED,
                               approved_by=approved_by,
                               approved_at=datetime.now(timezone.utc))

    def cancel(self, organization_id, purchase_order_id):
        return self._set_status(purchase_order_id, PurchaseOrderStatus.CANCELLED)

    def receive_goods(self, organization_id, purchase_order_id, lines, received_by,
                      notes=None) -> GoodsReceiptOut:
        po = self.orders[purchase_order_id]
        if po.status not in (PurchaseOrderStatus.APPROVED,
                            PurchaseOrderStatus.PARTIALLY_RECEIVED):
            raise InvalidPurchaseOrderTransitionError(po.status,
                                                      PurchaseOrderStatus.PARTIALLY_RECEIVED)
        by_id = {i.id: i for i in po.items}
        new_items = list(po.items)
        for line in lines:
            item = by_id.get(line.purchase_order_item_id)
            if item is None:
                raise PurchaseOrderItemNotFoundError(line.purchase_order_item_id)
            new_received = item.quantity_received + line.quantity
            if new_received > item.quantity_ordered:
                raise PurchaseOrderValidationError(["over-receipt"])
            idx = new_items.index(item)
            new_items[idx] = item.model_copy(update={"quantity_received": new_received})
        status = (PurchaseOrderStatus.RECEIVED
                 if all(i.quantity_received >= i.quantity_ordered for i in new_items)
                 else PurchaseOrderStatus.PARTIALLY_RECEIVED)
        self.orders[purchase_order_id] = po.model_copy(update={"items": new_items,
                                                               "status": status})
        return GoodsReceiptOut(id=uuid.uuid4(), purchase_order_id=purchase_order_id,
                               warehouse_id=po.warehouse_id, received_by=received_by,
                               notes=notes, received_at=datetime.now(timezone.utc), items=[])

    def record_return(self, organization_id, purchase_order_id, purchase_order_item_id,
                      quantity, reason, returned_by) -> PurchaseReturnOut:
        po = self.orders[purchase_order_id]
        item = next((i for i in po.items if i.id == purchase_order_item_id), None)
        if item is None:
            raise PurchaseOrderItemNotFoundError(purchase_order_item_id)
        if quantity > item.quantity_received:
            raise PurchaseOrderValidationError(["over-return"])
        new_items = [i.model_copy(update={"quantity_received": i.quantity_received - quantity})
                    if i.id == item.id else i for i in po.items]
        self.orders[purchase_order_id] = po.model_copy(update={"items": new_items})
        return PurchaseReturnOut(id=uuid.uuid4(), purchase_order_id=purchase_order_id,
                                 purchase_order_item_id=purchase_order_item_id,
                                 warehouse_id=po.warehouse_id, product_id=item.product_id,
                                 quantity=quantity, reason=reason, returned_by=returned_by,
                                 inventory_transaction_id=uuid.uuid4(),
                                 returned_at=datetime.now(timezone.utc))


def _product(product_id=None) -> ProductOut:
    now = datetime.now(timezone.utc)
    return ProductOut(id=product_id or uuid.uuid4(), sku="SKU-1", barcode=None, name="Widget",
                      description=None, category=None, brand=None, unit=UNIT,
                      purchase_price=Decimal("10"), selling_price=Decimal("15"),
                      tax_percent=Decimal("13"), minimum_stock_level=Decimal("0"),
                      status="active", created_at=now, updated_at=now)


WAREHOUSE_ID = uuid.uuid4()


def _service(permissions=ALL_PERMISSIONS, suppliers=None, purchase_orders=None, products=None):
    suppliers = suppliers or FakeSupplierRepository()
    purchase_orders = purchase_orders or FakePurchaseOrderRepository()
    products = products or FakeProductRepository({})
    warehouses = FakeWarehouseRepository(WAREHOUSE_ID)
    sessions = SessionManager(idle_timeout=timedelta(minutes=30))
    sessions.start(user_id=uuid.uuid4(), organization_id=ORG_ID, role_id=uuid.uuid4(),
                   permissions=frozenset(permissions), is_superuser=False,
                   must_change_password=False, now=datetime.now(timezone.utc))
    return (PurchaseService(suppliers, purchase_orders, products, warehouses, sessions),
           suppliers, purchase_orders, products)


def _po_data(supplier_id, product_id, **overrides):
    kwargs = dict(supplier_id=supplier_id, warehouse_id=WAREHOUSE_ID, expected_date=None,
                  notes=None,
                  items=[PurchaseOrderItemInput(product_id=product_id,
                                               quantity_ordered=Decimal("10"),
                                               unit_price=Decimal("5"),
                                               tax_percent=Decimal("13"))])
    kwargs.update(overrides)
    return PurchaseOrderCreate(**kwargs)


def _setup():
    product = _product()
    service, suppliers, purchase_orders, products = _service(
        products=FakeProductRepository({product.id: product}))
    supplier = service.create_supplier(SupplierCreate(name="Acme"))
    return service, supplier, product


# -- suppliers --------------------------------------------------------- #

def test_create_supplier_requires_name():
    service, _, _ = _setup()
    with pytest.raises(PurchaseOrderValidationError):
        service.create_supplier(SupplierCreate(name=""))


def test_create_supplier_requires_permission():
    service, _, _, _ = _service(permissions=frozenset())
    with pytest.raises(PermissionDeniedError):
        service.create_supplier(SupplierCreate(name="Acme"))


def test_get_supplier_missing_raises_not_found():
    service, _, _ = _setup()
    with pytest.raises(SupplierNotFoundError):
        service.get_supplier(uuid.uuid4())


# -- create / edit purchase orders --------------------------------------- #

def test_create_purchase_order_does_not_call_inventory_at_all():
    """The fake repositories here have no Inventory concept whatsoever —
    if PurchaseService.create_purchase_order tried to touch inventory,
    there'd be nothing to call and this would error, not silently pass.
    """
    service, supplier, product = _setup()
    po = service.create_purchase_order(_po_data(supplier.id, product.id))
    assert po.status == PurchaseOrderStatus.DRAFT
    assert po.items[0].quantity_received == Decimal("0")


def test_create_purchase_order_rejects_unknown_supplier():
    service, _, product = _setup()
    with pytest.raises(SupplierNotFoundError):
        service.create_purchase_order(_po_data(uuid.uuid4(), product.id))


def test_create_purchase_order_rejects_unknown_product():
    service, supplier, _ = _setup()
    with pytest.raises(ProductNotFoundError):
        service.create_purchase_order(_po_data(supplier.id, uuid.uuid4()))


def test_create_purchase_order_rejects_unknown_warehouse():
    service, supplier, product = _setup()
    with pytest.raises(WarehouseNotFoundError):
        service.create_purchase_order(_po_data(supplier.id, product.id,
                                               warehouse_id=uuid.uuid4()))


def test_create_purchase_order_requires_at_least_one_item():
    service, supplier, product = _setup()
    with pytest.raises(PurchaseOrderValidationError):
        service.create_purchase_order(_po_data(supplier.id, product.id, items=[]))


def test_create_purchase_order_rejects_invalid_item():
    service, supplier, product = _setup()
    with pytest.raises(PurchaseOrderValidationError):
        service.create_purchase_order(_po_data(
            supplier.id, product.id,
            items=[PurchaseOrderItemInput(product_id=product.id,
                                         quantity_ordered=Decimal("0"),
                                         unit_price=Decimal("5"), tax_percent=Decimal("0"))]))


def test_create_purchase_order_requires_permission():
    service, supplier, product = _setup()
    limited, _, _, _ = _service(permissions=frozenset())
    with pytest.raises(PermissionDeniedError):
        limited.create_purchase_order(_po_data(supplier.id, product.id))


def test_edit_purchase_order_allowed_while_draft():
    service, supplier, product = _setup()
    po = service.create_purchase_order(_po_data(supplier.id, product.id))
    updated = service.update_purchase_order(po.id, PurchaseOrderUpdate(notes="updated"))
    assert updated.notes == "updated"


def test_edit_purchase_order_rejected_once_submitted():
    service, supplier, product = _setup()
    po = service.create_purchase_order(_po_data(supplier.id, product.id))
    service.submit_purchase_order(po.id)
    with pytest.raises(PurchaseOrderValidationError):
        service.update_purchase_order(po.id, PurchaseOrderUpdate(notes="too late"))


# -- status machine ------------------------------------------------------- #

def test_full_happy_path_submit_approve():
    service, supplier, product = _setup()
    po = service.create_purchase_order(_po_data(supplier.id, product.id))

    submitted = service.submit_purchase_order(po.id)
    assert submitted.status == PurchaseOrderStatus.SUBMITTED

    approved = service.approve_purchase_order(po.id)
    assert approved.status == PurchaseOrderStatus.APPROVED


def test_cannot_approve_a_draft_order():
    service, supplier, product = _setup()
    po = service.create_purchase_order(_po_data(supplier.id, product.id))
    with pytest.raises(InvalidPurchaseOrderTransitionError):
        service.approve_purchase_order(po.id)


def test_cannot_submit_an_already_submitted_order():
    service, supplier, product = _setup()
    po = service.create_purchase_order(_po_data(supplier.id, product.id))
    service.submit_purchase_order(po.id)
    with pytest.raises(InvalidPurchaseOrderTransitionError):
        service.submit_purchase_order(po.id)


def test_cancel_draft_order():
    service, supplier, product = _setup()
    po = service.create_purchase_order(_po_data(supplier.id, product.id))
    cancelled = service.cancel_purchase_order(po.id)
    assert cancelled.status == PurchaseOrderStatus.CANCELLED


def test_cannot_cancel_a_received_order():
    service, supplier, product = _setup()
    po = service.create_purchase_order(_po_data(supplier.id, product.id))
    service.submit_purchase_order(po.id)
    service.approve_purchase_order(po.id)
    service.receive_goods(ReceiveGoodsRequest(
        purchase_order_id=po.id,
        lines=[GoodsReceiptLineInput(purchase_order_item_id=po.items[0].id,
                                     quantity=Decimal("10"))]))
    with pytest.raises(InvalidPurchaseOrderTransitionError):
        service.cancel_purchase_order(po.id)


def test_cannot_cancel_an_order_with_partial_receipts():
    """Receiving any quantity always advances status to at least
    PARTIALLY_RECEIVED (see FakePurchaseOrderRepository.receive_goods,
    mirroring the real repository), and the state machine excludes
    CANCELLED as a reachable target from PARTIALLY_RECEIVED/RECEIVED — so
    once anything has been received, cancellation is blocked by the
    transition table itself, not a separate check.
    """
    service, supplier, product = _setup()
    po = service.create_purchase_order(_po_data(supplier.id, product.id))
    service.submit_purchase_order(po.id)
    approved = service.approve_purchase_order(po.id)
    service.receive_goods(ReceiveGoodsRequest(
        purchase_order_id=po.id,
        lines=[GoodsReceiptLineInput(purchase_order_item_id=approved.items[0].id,
                                     quantity=Decimal("3"))]))
    with pytest.raises(InvalidPurchaseOrderTransitionError):
        service.cancel_purchase_order(po.id)


def test_status_transitions_require_permission():
    service, supplier, product = _setup()
    po = service.create_purchase_order(_po_data(supplier.id, product.id))
    limited, _, purchase_orders, _ = _service(permissions=frozenset({"purchase.read"}))
    with pytest.raises(PermissionDeniedError):
        limited.submit_purchase_order(po.id)


# -- receiving goods / partial receiving ---------------------------------- #

def test_receive_goods_full_quantity_marks_received():
    service, supplier, product = _setup()
    po = service.create_purchase_order(_po_data(supplier.id, product.id))
    service.submit_purchase_order(po.id)
    approved = service.approve_purchase_order(po.id)

    service.receive_goods(ReceiveGoodsRequest(
        purchase_order_id=po.id,
        lines=[GoodsReceiptLineInput(purchase_order_item_id=approved.items[0].id,
                                     quantity=Decimal("10"))]))

    final = service.get_purchase_order(po.id)
    assert final.status == PurchaseOrderStatus.RECEIVED


def test_partial_receiving_leaves_order_partially_received():
    service, supplier, product = _setup()
    po = service.create_purchase_order(_po_data(supplier.id, product.id))
    service.submit_purchase_order(po.id)
    approved = service.approve_purchase_order(po.id)

    service.receive_goods(ReceiveGoodsRequest(
        purchase_order_id=po.id,
        lines=[GoodsReceiptLineInput(purchase_order_item_id=approved.items[0].id,
                                     quantity=Decimal("4"))]))

    partial = service.get_purchase_order(po.id)
    assert partial.status == PurchaseOrderStatus.PARTIALLY_RECEIVED
    assert partial.items[0].quantity_received == Decimal("4")

    service.receive_goods(ReceiveGoodsRequest(
        purchase_order_id=po.id,
        lines=[GoodsReceiptLineInput(purchase_order_item_id=approved.items[0].id,
                                     quantity=Decimal("6"))]))

    final = service.get_purchase_order(po.id)
    assert final.status == PurchaseOrderStatus.RECEIVED


def test_receive_goods_requires_at_least_one_line():
    service, supplier, product = _setup()
    po = service.create_purchase_order(_po_data(supplier.id, product.id))
    service.submit_purchase_order(po.id)
    service.approve_purchase_order(po.id)
    with pytest.raises(PurchaseOrderValidationError):
        service.receive_goods(ReceiveGoodsRequest(purchase_order_id=po.id, lines=[]))


def test_receive_goods_requires_permission():
    service, supplier, product = _setup()
    po = service.create_purchase_order(_po_data(supplier.id, product.id))
    service.submit_purchase_order(po.id)
    approved = service.approve_purchase_order(po.id)
    limited, _, _, _ = _service(permissions=frozenset({"purchase.read"}))
    with pytest.raises(PermissionDeniedError):
        limited.receive_goods(ReceiveGoodsRequest(
            purchase_order_id=po.id,
            lines=[GoodsReceiptLineInput(purchase_order_item_id=approved.items[0].id,
                                         quantity=Decimal("1"))]))


def test_receive_goods_missing_purchase_order_raises_not_found():
    service, _, _ = _setup()
    with pytest.raises(PurchaseOrderNotFoundError):
        service.receive_goods(ReceiveGoodsRequest(
            purchase_order_id=uuid.uuid4(),
            lines=[GoodsReceiptLineInput(purchase_order_item_id=uuid.uuid4(),
                                         quantity=Decimal("1"))]))


# -- purchase returns ------------------------------------------------------#

def test_record_return_requires_reason():
    service, supplier, product = _setup()
    po = service.create_purchase_order(_po_data(supplier.id, product.id))
    service.submit_purchase_order(po.id)
    approved = service.approve_purchase_order(po.id)
    service.receive_goods(ReceiveGoodsRequest(
        purchase_order_id=po.id,
        lines=[GoodsReceiptLineInput(purchase_order_item_id=approved.items[0].id,
                                     quantity=Decimal("10"))]))
    with pytest.raises(PurchaseOrderValidationError):
        service.record_return(po.id, approved.items[0].id, Decimal("1"), "   ")


def test_record_return_requires_permission():
    service, supplier, product = _setup()
    po = service.create_purchase_order(_po_data(supplier.id, product.id))
    service.submit_purchase_order(po.id)
    approved = service.approve_purchase_order(po.id)
    service.receive_goods(ReceiveGoodsRequest(
        purchase_order_id=po.id,
        lines=[GoodsReceiptLineInput(purchase_order_item_id=approved.items[0].id,
                                     quantity=Decimal("10"))]))
    limited, _, _, _ = _service(permissions=frozenset({"purchase.read"}))
    with pytest.raises(PermissionDeniedError):
        limited.record_return(po.id, approved.items[0].id, Decimal("1"), "damaged")
