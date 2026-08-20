"""SalesService tested against hand-written fake repositories — no
database, no real locking. Proves validation, existence checks, the
status-machine's illegal-transition guard, and sales.*
permission enforcement happen in the service. Real row-locking/atomicity
across inventory + order status + invoice numbering + audit log is proven
against a live database in tests/repositories/test_sql_sales_repository.py
— a fake repository can't exercise that, it's plain dicts.
"""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.core.exceptions import (
    CreditLimitExceededError,
    CustomerNotFoundError,
    DuplicateCustomerCodeError,
    InvalidSalesOrderTransitionError,
    ProductNotFoundError,
    SalesOrderItemNotFoundError,
    SalesOrderNotFoundError,
    SalesOrderValidationError,
    WarehouseNotFoundError,
)
from app.domain.sales import PaymentMethod, SalesOrderStatus
from app.schemas.inventory import WarehouseOut
from app.schemas.product import ProductOut, UnitOut
from app.schemas.sales import (
    CustomerCreate,
    CustomerOut,
    CustomerUpdate,
    FinalizeSaleRequest,
    InvoiceOut,
    PaymentOut,
    PaymentRequest,
    SalesOrderCreate,
    SalesOrderItemInput,
    SalesOrderItemOut,
    SalesOrderOut,
    SalesOrderUpdate,
    SalesReturnOut,
)
from app.security.authorization import PermissionDeniedError
from app.security.session import SessionManager
from app.services.sales_service import SalesService

ORG_ID = uuid.uuid4()
UNIT = UnitOut(id=uuid.uuid4(), name="Piece", abbreviation="pc")

ALL_PERMISSIONS = frozenset({"sales.create", "sales.view", "sales.update", "sales.confirm",
                             "sales.fulfill", "sales.cancel", "sales.invoice", "sales.payment",
                             "sales.refund", "customers.view", "customers.create",
                             "customers.update", "customers.deactivate", "customers.export"})
WAREHOUSE_ID = uuid.uuid4()


class FakeAuditLogRepository:
    def __init__(self):
        self.entries: list[dict] = []

    def record(self, *, organization_id, user_id, actor_email, organization_name, action,
              entity_type=None, entity_id=None, changes=None):
        self.entries.append({"organization_id": organization_id, "user_id": user_id,
                            "actor_email": actor_email, "organization_name": organization_name,
                            "action": action, "entity_type": entity_type,
                            "entity_id": entity_id, "changes": changes})


class FakeCustomerRepository:
    def __init__(self):
        self.customers: dict[uuid.UUID, CustomerOut] = {}
        # Lets a test hand back a specific invoiced/paid/outstanding shape
        # without wiring cross-repository awareness into this fake — set
        # balance_overrides[customer_id] = CustomerBalance(...) directly.
        self.balance_overrides: dict[uuid.UUID, object] = {}

    def create(self, organization_id, data: CustomerCreate, created_by=None) -> CustomerOut:
        now = datetime.now(timezone.utc)
        customer = CustomerOut(
            id=uuid.uuid4(), customer_code=data.customer_code, name=data.name,
            customer_type=data.customer_type, contact_person=data.contact_person,
            phone=data.phone, email=data.email, address=data.address, city=data.city,
            state=data.state, country=data.country, tax_id=data.tax_id,
            credit_limit=data.credit_limit, opening_balance=data.opening_balance,
            notes=data.notes, is_active=True, created_by=created_by, created_at=now,
            updated_at=now)
        self.customers[customer.id] = customer
        return customer

    def update(self, organization_id, customer_id, data: CustomerUpdate):
        existing = self.customers.get(customer_id)
        if existing is None:
            return None
        updated = existing.model_copy(update=data.model_dump(exclude_unset=True))
        self.customers[customer_id] = updated
        return updated

    def get_by_id(self, organization_id, customer_id):
        return self.customers.get(customer_id)

    def list_all(self, organization_id):
        return list(self.customers.values())

    def code_exists(self, organization_id, code, exclude_id=None):
        return any(c.customer_code == code for c in self.customers.values()
                  if c.id != exclude_id)

    def get_balance(self, organization_id, customer_id):
        from app.schemas.sales import CustomerBalance
        if customer_id in self.balance_overrides:
            return self.balance_overrides[customer_id]
        customer = self.customers.get(customer_id)
        if customer is None:
            return None
        return CustomerBalance(customer_id=customer_id,
                               opening_balance=customer.opening_balance,
                               invoiced_total=Decimal("0"), pending_orders_total=Decimal("0"),
                               paid_total=Decimal("0"),
                               outstanding_balance=customer.opening_balance,
                               credit_limit=customer.credit_limit)

    def get_history(self, organization_id, customer_id, limit=100):
        return []


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


class FakeSalesOrderRepository:
    """Obedient — does exactly what's asked with no independent business
    rules, so tests can tell whether an assertion failure means the
    SERVICE didn't validate/gate something (this fake would have let it
    through) rather than the fake silently fixing it up.
    """

    def __init__(self):
        self.orders: dict[uuid.UUID, SalesOrderOut] = {}
        self.invoices: dict[uuid.UUID, InvoiceOut] = {}
        self._next_invoice_seq = 1

    def create(self, organization_id, data: SalesOrderCreate, created_by) -> SalesOrderOut:
        now = datetime.now(timezone.utc)
        items = [SalesOrderItemOut(id=uuid.uuid4(), product_id=i.product_id,
                                   quantity_ordered=i.quantity_ordered,
                                   quantity_fulfilled=Decimal("0"), unit_price=i.unit_price,
                                   tax_percent=i.tax_percent,
                                   discount_percent=i.discount_percent)
                for i in data.items]
        so = SalesOrderOut(id=uuid.uuid4(), customer_id=data.customer_id,
                           warehouse_id=data.warehouse_id, status=SalesOrderStatus.DRAFT,
                           notes=data.notes, created_by=created_by, confirmed_by=None,
                           confirmed_at=None, items=items, created_at=now, updated_at=now)
        self.orders[so.id] = so
        return so

    def update(self, organization_id, sales_order_id, data: SalesOrderUpdate):
        existing = self.orders.get(sales_order_id)
        if existing is None:
            return None
        updates = data.model_dump(exclude_unset=True, exclude={"items"})
        items = existing.items
        if data.items is not None:
            items = [SalesOrderItemOut(id=uuid.uuid4(), product_id=i.product_id,
                                       quantity_ordered=i.quantity_ordered,
                                       quantity_fulfilled=Decimal("0"),
                                       unit_price=i.unit_price, tax_percent=i.tax_percent,
                                       discount_percent=i.discount_percent)
                    for i in data.items]
        updated = existing.model_copy(update={**updates, "items": items})
        self.orders[sales_order_id] = updated
        return updated

    def get_by_id(self, organization_id, sales_order_id):
        return self.orders.get(sales_order_id)

    def search(self, organization_id, filter):
        raise NotImplementedError  # not exercised by these tests

    def confirm(self, organization_id, sales_order_id, confirmed_by):
        existing = self.orders[sales_order_id]
        updated = existing.model_copy(update={"status": SalesOrderStatus.CONFIRMED,
                                              "confirmed_by": confirmed_by,
                                              "confirmed_at": datetime.now(timezone.utc)})
        self.orders[sales_order_id] = updated
        return updated

    def cancel(self, organization_id, sales_order_id, cancelled_by):
        existing = self.orders[sales_order_id]
        updated = existing.model_copy(update={"status": SalesOrderStatus.CANCELLED})
        self.orders[sales_order_id] = updated
        return updated

    def fulfill_sale(self, organization_id, sales_order_id, fulfilled_by) -> SalesOrderOut:
        so = self.orders[sales_order_id]
        if so.status != SalesOrderStatus.CONFIRMED:
            raise InvalidSalesOrderTransitionError(so.status, SalesOrderStatus.FULFILLED)
        new_items = [i.model_copy(update={"quantity_fulfilled": i.quantity_ordered})
                    for i in so.items]
        updated = so.model_copy(update={"items": new_items,
                                        "status": SalesOrderStatus.FULFILLED})
        self.orders[sales_order_id] = updated
        return updated

    def generate_invoice(self, organization_id, sales_order_id, generated_by) -> InvoiceOut:
        so = self.orders[sales_order_id]
        subtotal = sum((i.quantity_ordered * i.unit_price for i in so.items), Decimal("0"))
        invoice = InvoiceOut(id=uuid.uuid4(), sales_order_id=so.id,
                             invoice_number=f"INV-{self._next_invoice_seq:06d}",
                             subtotal=subtotal, discount_amount=Decimal("0"),
                             overall_discount_amount=Decimal("0"),
                             tax_amount=Decimal("0"), other_charges=Decimal("0"),
                             total_amount=subtotal, due_date=None, generated_by=generated_by,
                             generated_at=datetime.now(timezone.utc))
        self._next_invoice_seq += 1
        self.invoices[invoice.id] = invoice
        return invoice

    def get_invoice(self, organization_id, invoice_id):
        return self.invoices.get(invoice_id)

    def get_invoice_by_sales_order(self, organization_id, sales_order_id):
        return next((inv for inv in self.invoices.values()
                    if inv.sales_order_id == sales_order_id), None)

    def record_payment(self, organization_id, data: PaymentRequest, recorded_by) -> PaymentOut:
        invoice = self.invoices[data.invoice_id]
        so = next(o for o in self.orders.values() if o.id == invoice.sales_order_id)
        if data.amount >= invoice.total_amount and so.status == SalesOrderStatus.FULFILLED:
            self.orders[so.id] = so.model_copy(update={"status": SalesOrderStatus.COMPLETED})
        return PaymentOut(id=uuid.uuid4(), invoice_id=invoice.id, amount=data.amount,
                          method=data.method, recorded_by=recorded_by, notes=data.notes,
                          received_at=datetime.now(timezone.utc))

    def finalize_new_bill(self, organization_id, sales_order_id, actor_id, data):
        so = self.orders[sales_order_id]
        if so.status != SalesOrderStatus.DRAFT:
            raise InvalidSalesOrderTransitionError(so.status, SalesOrderStatus.CONFIRMED)
        self.confirm(organization_id, sales_order_id, actor_id)
        self.fulfill_sale(organization_id, sales_order_id, actor_id)
        invoice = self.generate_invoice(organization_id, sales_order_id, actor_id)
        if data.due_date is not None or data.overall_discount_amount or data.other_charges:
            invoice = invoice.model_copy(update={
                "due_date": data.due_date,
                "overall_discount_amount": data.overall_discount_amount,
                "other_charges": data.other_charges,
                "total_amount": (invoice.total_amount - data.overall_discount_amount
                                + data.other_charges)})
            self.invoices[invoice.id] = invoice
        payment = None
        if data.payment_amount is not None:
            payment = self.record_payment(
                organization_id,
                PaymentRequest(invoice_id=invoice.id, amount=data.payment_amount,
                              method=data.payment_method, notes=data.payment_notes),
                actor_id)
        from app.schemas.sales import FinalizeSaleResult
        return FinalizeSaleResult(sales_order=self.orders[sales_order_id], invoice=invoice,
                                  payment=payment)

    def record_return(self, organization_id, sales_order_id, sales_order_item_id, quantity,
                      reason, returned_by) -> SalesReturnOut:
        so = self.orders[sales_order_id]
        item = next((i for i in so.items if i.id == sales_order_item_id), None)
        if item is None:
            raise SalesOrderItemNotFoundError(sales_order_item_id)
        if quantity > item.quantity_fulfilled:
            raise SalesOrderValidationError(["over-return"])
        new_items = [i.model_copy(update={"quantity_fulfilled": i.quantity_fulfilled - quantity})
                    if i.id == item.id else i for i in so.items]
        self.orders[sales_order_id] = so.model_copy(update={"items": new_items})
        return SalesReturnOut(id=uuid.uuid4(), sales_order_id=sales_order_id,
                              sales_order_item_id=sales_order_item_id,
                              warehouse_id=so.warehouse_id, product_id=item.product_id,
                              quantity=quantity, reason=reason, returned_by=returned_by,
                              inventory_transaction_id=uuid.uuid4(),
                              returned_at=datetime.now(timezone.utc))


def _product(product_id=None) -> ProductOut:
    now = datetime.now(timezone.utc)
    return ProductOut(id=product_id or uuid.uuid4(), sku="SKU-1", barcode=None, name="Widget",
                      description=None, product_type="goods", category=None, brand=None,
                      unit=UNIT, sub_unit=None, sub_unit_conversion_factor=None,
                      tertiary_unit=None, tertiary_unit_conversion_factor=None,
                      purchase_price=Decimal("10"), selling_price=Decimal("15"),
                      tax_percent=Decimal("13"), is_taxable=True,
                      minimum_stock_level=Decimal("0"), hsn_code=None, size=None, color=None,
                      flavour=None, dftqc_no=None, country_of_origin=None, expiry_date=None,
                      status="active", created_at=now, updated_at=now)


def _service(permissions=ALL_PERMISSIONS, customers=None, sales_orders=None, products=None):
    customers = customers or FakeCustomerRepository()
    sales_orders = sales_orders or FakeSalesOrderRepository()
    products = products or FakeProductRepository({})
    warehouses = FakeWarehouseRepository(WAREHOUSE_ID)
    sessions = SessionManager(idle_timeout=timedelta(minutes=30))
    sessions.start(user_id=uuid.uuid4(), organization_id=ORG_ID, role_id=uuid.uuid4(),
                   permissions=frozenset(permissions), is_superuser=False,
                   must_change_password=False, now=datetime.now(timezone.utc))
    audit_log = FakeAuditLogRepository()
    return (SalesService(customers, sales_orders, products, warehouses, sessions, audit_log),
           customers, sales_orders, products, audit_log)


def _so_data(customer_id, product_id, **overrides):
    kwargs = dict(customer_id=customer_id, warehouse_id=WAREHOUSE_ID, notes=None,
                  items=[SalesOrderItemInput(product_id=product_id,
                                            quantity_ordered=Decimal("10"),
                                            unit_price=Decimal("5"),
                                            tax_percent=Decimal("13"))])
    kwargs.update(overrides)
    return SalesOrderCreate(**kwargs)


def _setup():
    product = _product()
    service, customers, sales_orders, products, _audit_log = _service(
        products=FakeProductRepository({product.id: product}))
    customer = service.create_customer(CustomerCreate(name="Jane"))
    return service, customer, product


# -- customers ----------------------------------------------------------- #

def test_create_customer_requires_name():
    service, _, _ = _setup()
    with pytest.raises(SalesOrderValidationError):
        service.create_customer(CustomerCreate(name=""))


def test_create_customer_requires_permission():
    service, _, _, _, _ = _service(permissions=frozenset())
    with pytest.raises(PermissionDeniedError):
        service.create_customer(CustomerCreate(name="Jane"))


def test_get_customer_missing_raises_not_found():
    service, _, _ = _setup()
    with pytest.raises(CustomerNotFoundError):
        service.get_customer(uuid.uuid4())


def test_create_customer_derives_code_from_name_when_none_given():
    service, _, _ = _setup()
    customer = service.create_customer(CustomerCreate(name="Acme Retail Co"))
    assert customer.customer_code == "ACME-RETAIL-CO"


def test_create_customer_normalizes_an_explicit_code():
    service, _, _ = _setup()
    customer = service.create_customer(CustomerCreate(name="Jane", customer_code="  jane-01  "))
    assert customer.customer_code == "JANE-01"


def test_create_customer_rejects_duplicate_explicit_code():
    service, _, _ = _setup()
    service.create_customer(CustomerCreate(name="First", customer_code="DUP"))
    with pytest.raises(DuplicateCustomerCodeError):
        service.create_customer(CustomerCreate(name="Second", customer_code="DUP"))


def test_create_customer_rejects_negative_credit_limit():
    service, _, _ = _setup()
    with pytest.raises(SalesOrderValidationError):
        service.create_customer(CustomerCreate(name="Jane", credit_limit=Decimal("-1")))


def test_create_customer_rejects_negative_opening_balance():
    service, _, _ = _setup()
    with pytest.raises(SalesOrderValidationError):
        service.create_customer(CustomerCreate(name="Jane", opening_balance=Decimal("-1")))


def test_create_customer_rejects_invalid_email():
    service, _, _ = _setup()
    with pytest.raises(SalesOrderValidationError):
        service.create_customer(CustomerCreate(name="Jane", email="not-an-email"))


def test_create_customer_rejects_invalid_phone():
    service, _, _ = _setup()
    with pytest.raises(SalesOrderValidationError):
        service.create_customer(CustomerCreate(name="Jane", phone="abc"))


def test_create_customer_sets_created_by_from_session():
    service, customers, sales_orders, products, _audit_log = _service()
    session = service._sessions.peek()
    customer = service.create_customer(CustomerCreate(name="Jane"))
    assert customer.created_by == session.user_id


def test_create_customer_records_audit_entry():
    service, customers, sales_orders, products, audit_log = _service()
    customer = service.create_customer(CustomerCreate(name="Jane", customer_code="JANE-01"))
    entries = [e for e in audit_log.entries if e["action"] == "customer.create"]
    assert len(entries) == 1
    assert entries[0]["entity_id"] == customer.id
    assert entries[0]["entity_type"] == "customer"


def test_update_customer_rejects_duplicate_code():
    service, _, _ = _setup()
    service.create_customer(CustomerCreate(name="Other", customer_code="TAKEN"))
    mine = service.create_customer(CustomerCreate(name="Mine", customer_code="MINE"))
    with pytest.raises(DuplicateCustomerCodeError):
        service.update_customer(mine.id, CustomerUpdate(customer_code="TAKEN"))


def test_update_customer_keeping_its_own_code_is_not_a_duplicate():
    service, _, _ = _setup()
    mine = service.create_customer(CustomerCreate(name="Mine", customer_code="MINE"))
    updated = service.update_customer(mine.id, CustomerUpdate(customer_code="mine",
                                                              city="Pokhara"))
    assert updated.customer_code == "MINE"
    assert updated.city == "Pokhara"


def test_update_customer_requires_permission():
    service, _, _, _, _ = _service(permissions=frozenset({"customers.view"}))
    with pytest.raises(PermissionDeniedError):
        service.update_customer(uuid.uuid4(), CustomerUpdate(name="X"))


def test_update_customer_records_generic_diff_and_credit_limit_changed_event():
    service, customers, sales_orders, products, audit_log = _service()
    customer = service.create_customer(CustomerCreate(name="Jane", credit_limit=Decimal("100")))
    audit_log.entries.clear()

    service.update_customer(customer.id, CustomerUpdate(credit_limit=Decimal("500")))

    generic = [e for e in audit_log.entries if e["action"] == "customer.update"]
    specific = [e for e in audit_log.entries if e["action"] == "customer.credit_limit_changed"]
    assert len(generic) == 1
    assert len(specific) == 1
    assert specific[0]["changes"] == {"before": "100", "after": "500"}


def test_deactivate_and_activate_customer_record_audit_entries():
    service, customers, sales_orders, products, audit_log = _service()
    customer = service.create_customer(CustomerCreate(name="Jane"))

    deactivated = service.deactivate_customer(customer.id)
    assert deactivated.is_active is False
    activated = service.activate_customer(customer.id)
    assert activated.is_active is True

    actions = [e["action"] for e in audit_log.entries]
    assert "customer.deactivate" in actions
    assert "customer.activate" in actions


def test_deactivate_customer_requires_permission():
    service, _, _, _, _ = _service(permissions=frozenset({"customers.view"}))
    with pytest.raises(PermissionDeniedError):
        service.deactivate_customer(uuid.uuid4())


def test_deactivate_missing_customer_raises_not_found():
    service, _, _ = _setup()
    with pytest.raises(CustomerNotFoundError):
        service.deactivate_customer(uuid.uuid4())


def test_get_customer_balance_requires_permission():
    service, _, _, _, _ = _service(permissions=frozenset())
    with pytest.raises(PermissionDeniedError):
        service.get_customer_balance(uuid.uuid4())


def test_get_customer_history_requires_permission():
    service, _, _, _, _ = _service(permissions=frozenset())
    with pytest.raises(PermissionDeniedError):
        service.get_customer_history(uuid.uuid4())


def test_get_customer_history_missing_customer_raises_not_found():
    service, _, _ = _setup()
    with pytest.raises(CustomerNotFoundError):
        service.get_customer_history(uuid.uuid4())


def test_export_customers_requires_customers_export_permission():
    service, _, _, _, _ = _service(permissions=frozenset({"customers.view"}))
    with pytest.raises(PermissionDeniedError):
        service.export_customers()


def test_export_customers_returns_the_full_list():
    service, customers, sales_orders, products, _audit_log = _service()
    service.create_customer(CustomerCreate(name="Jane"))
    service.create_customer(CustomerCreate(name="John"))
    exported = service.export_customers()
    assert {c.name for c in exported} == {"Jane", "John"}


# -- credit control ---------------------------------------------------------#

def test_create_sales_order_blocked_when_it_would_exceed_credit_limit():
    from app.schemas.sales import CustomerBalance
    product = _product()
    service, customers, sales_orders, products = _service(
        products=FakeProductRepository({product.id: product}))[:4]
    customer = service.create_customer(CustomerCreate(name="Jane", credit_limit=Decimal("100")))
    customers.balance_overrides[customer.id] = CustomerBalance(
        customer_id=customer.id, opening_balance=Decimal("0"), invoiced_total=Decimal("80"),
        pending_orders_total=Decimal("0"), paid_total=Decimal("0"),
        outstanding_balance=Decimal("80"), credit_limit=Decimal("100"))

    data = _so_data(customer.id, product.id, items=[
        SalesOrderItemInput(product_id=product.id, quantity_ordered=Decimal("1"),
                            unit_price=Decimal("30"), tax_percent=Decimal("0"))])
    with pytest.raises(CreditLimitExceededError):
        service.create_sales_order(data)
    assert sales_orders.orders == {}  # nothing was created


def test_create_sales_order_allowed_within_credit_limit():
    from app.schemas.sales import CustomerBalance
    product = _product()
    service, customers, sales_orders, products = _service(
        products=FakeProductRepository({product.id: product}))[:4]
    customer = service.create_customer(CustomerCreate(name="Jane", credit_limit=Decimal("100")))
    customers.balance_overrides[customer.id] = CustomerBalance(
        customer_id=customer.id, opening_balance=Decimal("0"), invoiced_total=Decimal("50"),
        pending_orders_total=Decimal("0"), paid_total=Decimal("0"),
        outstanding_balance=Decimal("50"), credit_limit=Decimal("100"))

    data = _so_data(customer.id, product.id, items=[
        SalesOrderItemInput(product_id=product.id, quantity_ordered=Decimal("1"),
                            unit_price=Decimal("30"), tax_percent=Decimal("0"))])
    so = service.create_sales_order(data)
    assert so.id in sales_orders.orders


def test_create_sales_order_never_blocked_when_customer_has_no_credit_limit():
    from app.schemas.sales import CustomerBalance
    product = _product()
    service, customers, sales_orders, products = _service(
        products=FakeProductRepository({product.id: product}))[:4]
    customer = service.create_customer(CustomerCreate(name="Jane"))  # credit_limit=None
    customers.balance_overrides[customer.id] = CustomerBalance(
        customer_id=customer.id, opening_balance=Decimal("0"), invoiced_total=Decimal("999999"),
        pending_orders_total=Decimal("0"), paid_total=Decimal("0"),
        outstanding_balance=Decimal("999999"), credit_limit=None)

    data = _so_data(customer.id, product.id, items=[
        SalesOrderItemInput(product_id=product.id, quantity_ordered=Decimal("1"),
                            unit_price=Decimal("30"), tax_percent=Decimal("0"))])
    so = service.create_sales_order(data)  # does not raise
    assert so.id in sales_orders.orders


# -- create / edit sales orders -------------------------------------------#

def test_create_sales_order_does_not_call_inventory_at_all():
    """The fake repositories here have no Inventory concept whatsoever —
    if SalesService.create_sales_order tried to touch inventory, there'd
    be nothing to call and this would error, not silently pass.
    """
    service, customer, product = _setup()
    so = service.create_sales_order(_so_data(customer.id, product.id))
    assert so.status == SalesOrderStatus.DRAFT
    assert so.items[0].quantity_fulfilled == Decimal("0")


def test_create_sales_order_rejects_unknown_customer():
    service, _, product = _setup()
    with pytest.raises(CustomerNotFoundError):
        service.create_sales_order(_so_data(uuid.uuid4(), product.id))


def test_create_sales_order_rejects_unknown_product():
    service, customer, _ = _setup()
    with pytest.raises(ProductNotFoundError):
        service.create_sales_order(_so_data(customer.id, uuid.uuid4()))


def test_create_sales_order_rejects_unknown_warehouse():
    service, customer, product = _setup()
    with pytest.raises(WarehouseNotFoundError):
        service.create_sales_order(_so_data(customer.id, product.id,
                                            warehouse_id=uuid.uuid4()))


def test_create_sales_order_requires_at_least_one_item():
    service, customer, product = _setup()
    with pytest.raises(SalesOrderValidationError):
        service.create_sales_order(_so_data(customer.id, product.id, items=[]))


def test_create_sales_order_rejects_invalid_item():
    service, customer, product = _setup()
    with pytest.raises(SalesOrderValidationError):
        service.create_sales_order(_so_data(
            customer.id, product.id,
            items=[SalesOrderItemInput(product_id=product.id, quantity_ordered=Decimal("0"),
                                      unit_price=Decimal("5"), tax_percent=Decimal("0"))]))


def test_create_sales_order_requires_permission():
    service, customer, product = _setup()
    limited, _, _, _, _ = _service(permissions=frozenset())
    with pytest.raises(PermissionDeniedError):
        limited.create_sales_order(_so_data(customer.id, product.id))


def test_edit_sales_order_allowed_while_draft():
    service, customer, product = _setup()
    so = service.create_sales_order(_so_data(customer.id, product.id))
    updated = service.update_sales_order(so.id, SalesOrderUpdate(notes="updated"))
    assert updated.notes == "updated"


def test_edit_sales_order_rejected_once_confirmed():
    service, customer, product = _setup()
    so = service.create_sales_order(_so_data(customer.id, product.id))
    service.confirm_sales_order(so.id)
    with pytest.raises(SalesOrderValidationError):
        service.update_sales_order(so.id, SalesOrderUpdate(notes="too late"))


# -- status machine -------------------------------------------------------- #

def test_full_happy_path_confirm_fulfill():
    service, customer, product = _setup()
    so = service.create_sales_order(_so_data(customer.id, product.id))

    confirmed = service.confirm_sales_order(so.id)
    assert confirmed.status == SalesOrderStatus.CONFIRMED

    fulfilled = service.fulfill_sale(so.id)
    assert fulfilled.status == SalesOrderStatus.FULFILLED


def test_cannot_fulfill_a_draft_order():
    service, customer, product = _setup()
    so = service.create_sales_order(_so_data(customer.id, product.id))
    with pytest.raises(InvalidSalesOrderTransitionError):
        service.fulfill_sale(so.id)


def test_cannot_confirm_an_already_confirmed_order():
    service, customer, product = _setup()
    so = service.create_sales_order(_so_data(customer.id, product.id))
    service.confirm_sales_order(so.id)
    with pytest.raises(InvalidSalesOrderTransitionError):
        service.confirm_sales_order(so.id)


def test_cancel_draft_order():
    service, customer, product = _setup()
    so = service.create_sales_order(_so_data(customer.id, product.id))
    cancelled = service.cancel_sales_order(so.id)
    assert cancelled.status == SalesOrderStatus.CANCELLED


def test_cannot_cancel_a_fulfilled_order():
    """The state machine excludes CANCELLED as a target from FULFILLED —
    once goods are out, cancellation isn't offered as an action at all,
    a sales return is the correct tool.
    """
    service, customer, product = _setup()
    so = service.create_sales_order(_so_data(customer.id, product.id))
    service.confirm_sales_order(so.id)
    service.fulfill_sale(so.id)
    with pytest.raises(InvalidSalesOrderTransitionError):
        service.cancel_sales_order(so.id)


def test_status_transitions_require_permission():
    service, customer, product = _setup()
    so = service.create_sales_order(_so_data(customer.id, product.id))
    limited, _, _, _, _ = _service(permissions=frozenset({"sales.view"}))
    with pytest.raises(PermissionDeniedError):
        limited.confirm_sales_order(so.id)


# -- invoicing / payment ---------------------------------------------------#

def _fulfilled(service, customer, product):
    so = service.create_sales_order(_so_data(customer.id, product.id))
    service.confirm_sales_order(so.id)
    return service.fulfill_sale(so.id)


def test_generate_invoice_requires_permission():
    service, customer, product = _setup()
    fulfilled = _fulfilled(service, customer, product)
    limited, _, _, _, _ = _service(permissions=frozenset({"sales.view"}))
    with pytest.raises(PermissionDeniedError):
        limited.generate_invoice(fulfilled.id)


def test_record_payment_rejects_non_positive_amount():
    service, customer, product = _setup()
    fulfilled = _fulfilled(service, customer, product)
    invoice = service.generate_invoice(fulfilled.id)
    with pytest.raises(SalesOrderValidationError):
        service.record_payment(PaymentRequest(invoice_id=invoice.id, amount=Decimal("0"),
                                              method=PaymentMethod.CASH))


def test_full_payment_completes_order():
    service, customer, product = _setup()
    fulfilled = _fulfilled(service, customer, product)
    invoice = service.generate_invoice(fulfilled.id)
    service.record_payment(PaymentRequest(invoice_id=invoice.id, amount=invoice.total_amount,
                                          method=PaymentMethod.CASH))
    final = service.get_sales_order(fulfilled.id)
    assert final.status == SalesOrderStatus.COMPLETED


def test_record_payment_requires_permission():
    service, customer, product = _setup()
    fulfilled = _fulfilled(service, customer, product)
    invoice = service.generate_invoice(fulfilled.id)
    limited, _, _, _, _ = _service(permissions=frozenset({"sales.view"}))
    with pytest.raises(PermissionDeniedError):
        limited.record_payment(PaymentRequest(invoice_id=invoice.id, amount=Decimal("1"),
                                              method=PaymentMethod.CASH))


# -- finalize_new_bill (New Bill: Save as Sale) ----------------------------#

def test_finalize_new_bill_confirms_fulfills_and_invoices_in_one_call():
    service, customer, product = _setup()
    so = service.create_sales_order(_so_data(customer.id, product.id))

    result = service.finalize_new_bill(so.id, FinalizeSaleRequest())

    assert result.sales_order.status == SalesOrderStatus.FULFILLED
    assert result.invoice is not None
    assert result.payment is None


def test_finalize_new_bill_with_payment_records_payment_and_completes_order():
    # _so_data's default line is qty=10 x unit_price=5 = 50; the fake
    # repository's generate_invoice applies no tax, so 50 is the exact
    # total — paying it in full must complete the order in the same call.
    service, customer, product = _setup()
    so = service.create_sales_order(_so_data(customer.id, product.id))

    result = service.finalize_new_bill(
        so.id, FinalizeSaleRequest(payment_amount=Decimal("50"),
                                   payment_method=PaymentMethod.CASH))

    assert result.payment is not None
    assert result.payment.amount == Decimal("50")
    assert result.sales_order.status == SalesOrderStatus.COMPLETED


def test_finalize_new_bill_rejects_non_draft_order():
    service, customer, product = _setup()
    so = service.create_sales_order(_so_data(customer.id, product.id))
    service.confirm_sales_order(so.id)  # no longer DRAFT

    with pytest.raises(SalesOrderValidationError):
        service.finalize_new_bill(so.id, FinalizeSaleRequest())


def test_finalize_new_bill_rejects_negative_overall_discount():
    service, customer, product = _setup()
    so = service.create_sales_order(_so_data(customer.id, product.id))

    with pytest.raises(SalesOrderValidationError):
        service.finalize_new_bill(
            so.id, FinalizeSaleRequest(overall_discount_amount=Decimal("-1")))


def test_finalize_new_bill_rejects_negative_other_charges():
    service, customer, product = _setup()
    so = service.create_sales_order(_so_data(customer.id, product.id))

    with pytest.raises(SalesOrderValidationError):
        service.finalize_new_bill(so.id, FinalizeSaleRequest(other_charges=Decimal("-1")))


def test_finalize_new_bill_rejects_non_positive_payment_amount():
    service, customer, product = _setup()
    so = service.create_sales_order(_so_data(customer.id, product.id))

    with pytest.raises(SalesOrderValidationError):
        service.finalize_new_bill(
            so.id, FinalizeSaleRequest(payment_amount=Decimal("0"),
                                       payment_method=PaymentMethod.CASH))


def test_finalize_new_bill_rejects_payment_amount_without_method():
    service, customer, product = _setup()
    so = service.create_sales_order(_so_data(customer.id, product.id))

    with pytest.raises(SalesOrderValidationError):
        service.finalize_new_bill(so.id, FinalizeSaleRequest(payment_amount=Decimal("10")))


@pytest.mark.parametrize("missing_permission", ["sales.confirm", "sales.fulfill", "sales.invoice"])
def test_finalize_new_bill_requires_confirm_fulfill_and_invoice_permissions(missing_permission):
    service, customer, product = _setup()
    so = service.create_sales_order(_so_data(customer.id, product.id))

    limited_permissions = (ALL_PERMISSIONS - {missing_permission})
    limited, _, limited_orders, _, _ = _service(permissions=limited_permissions)
    # Share the same order across services the way a shared org DB would —
    # simplest is to just copy the fake's in-memory order into the limited
    # service's own repository instance.
    limited_orders.orders[so.id] = service._sales_orders.orders[so.id]

    with pytest.raises(PermissionDeniedError):
        limited.finalize_new_bill(so.id, FinalizeSaleRequest())


def test_finalize_new_bill_requires_payment_permission_when_payment_included():
    service, customer, product = _setup()
    so = service.create_sales_order(_so_data(customer.id, product.id))

    limited_permissions = (ALL_PERMISSIONS - {"sales.payment"})
    limited, _, limited_orders, _, _ = _service(permissions=limited_permissions)
    limited_orders.orders[so.id] = service._sales_orders.orders[so.id]

    with pytest.raises(PermissionDeniedError):
        limited.finalize_new_bill(
            so.id, FinalizeSaleRequest(payment_amount=Decimal("10"),
                                       payment_method=PaymentMethod.CASH))


def test_finalize_new_bill_missing_sales_order_raises_not_found():
    service, _, _ = _setup()
    with pytest.raises(SalesOrderNotFoundError):
        service.finalize_new_bill(uuid.uuid4(), FinalizeSaleRequest())


# -- sales returns ----------------------------------------------------------#

def test_record_return_requires_reason():
    service, customer, product = _setup()
    fulfilled = _fulfilled(service, customer, product)
    with pytest.raises(SalesOrderValidationError):
        service.record_sales_return(fulfilled.id, fulfilled.items[0].id, Decimal("1"), "  ")


def test_record_return_requires_permission():
    service, customer, product = _setup()
    fulfilled = _fulfilled(service, customer, product)
    limited, _, _, _, _ = _service(permissions=frozenset({"sales.view"}))
    with pytest.raises(PermissionDeniedError):
        limited.record_sales_return(fulfilled.id, fulfilled.items[0].id, Decimal("1"),
                                    "damaged")


def test_record_return_missing_sales_order_raises_not_found():
    service, _, _ = _setup()
    with pytest.raises(SalesOrderNotFoundError):
        service.record_sales_return(uuid.uuid4(), uuid.uuid4(), Decimal("1"), "damaged")


# -- get_invoice_by_sales_order --------------------------------------------#

def test_get_invoice_by_sales_order_returns_none_before_invoicing():
    service, customer, product = _setup()
    fulfilled = _fulfilled(service, customer, product)
    assert service.get_invoice_by_sales_order(fulfilled.id) is None


def test_get_invoice_by_sales_order_returns_the_invoice_once_generated():
    service, customer, product = _setup()
    fulfilled = _fulfilled(service, customer, product)
    invoice = service.generate_invoice(fulfilled.id)

    found = service.get_invoice_by_sales_order(fulfilled.id)

    assert found is not None
    assert found.id == invoice.id


def test_get_invoice_by_sales_order_requires_permission():
    service, customer, product = _setup()
    fulfilled = _fulfilled(service, customer, product)
    limited, _, _, _, _ = _service(permissions=frozenset())
    with pytest.raises(PermissionDeniedError):
        limited.get_invoice_by_sales_order(fulfilled.id)
