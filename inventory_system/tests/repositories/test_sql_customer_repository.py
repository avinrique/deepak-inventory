"""SqlCustomerRepository against a live PostgreSQL database — proves the
customer-code unique constraint actually rolls back cleanly at the DB
level (not just the service-layer pre-check), and that get_balance/
get_history correctly aggregate real SalesOrder/Invoice/Payment/SalesReturn
rows written by SqlSalesOrderRepository/SqlInventoryRepository — i.e. that
Billing's data really is the only source of truth Customer Management
reads from, end to end, not just in the service-layer fakes.

Uses the ``live_db`` fixture (tests/conftest.py) — see its docstring for
why this is gated on INVENTORY_TEST_DATABASE_URL and how to run these
locally.
"""
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from app.database.session import get_session
from app.domain.sales import CustomerType, SalesOrderStatus
from app.models import Customer, Organization, Product, Unit, User, Warehouse
from app.repositories.sql.customer_repository import SqlCustomerRepository
from app.repositories.sql.inventory_repository import SqlInventoryRepository
from app.repositories.sql.sales_repository import SqlSalesOrderRepository
from app.schemas.sales import (
    CustomerCreate,
    CustomerUpdate,
    PaymentRequest,
    SalesOrderCreate,
    SalesOrderItemInput,
)


@pytest.fixture()
def world(live_db):
    with get_session() as session:
        org = Organization(name="Acme Retail")
        session.add(org)
        session.flush()
        unit = Unit(organization_id=org.id, name="Piece", abbreviation="pc")
        session.add(unit)
        session.flush()
        product = Product(organization_id=org.id, sku="SKU-1", name="Widget", unit_id=unit.id,
                          purchase_price=Decimal("10"), selling_price=Decimal("15"),
                          tax_percent=Decimal("0"), minimum_stock_level=Decimal("0"))
        warehouse = Warehouse(organization_id=org.id, code="MAIN", name="Main")
        user = User(email="clerk@example.com", username="clerk", hashed_password="x",
                   full_name="Clerk")
        session.add_all([product, warehouse, user])
        session.flush()
        return {"org_id": org.id, "product_id": product.id, "warehouse_id": warehouse.id,
               "user_id": user.id}


def _repo():
    return SqlCustomerRepository()


def _create_customer(world, **overrides):
    kwargs = dict(name="Jane Buyer", customer_code=None, customer_type=CustomerType.INDIVIDUAL,
                 contact_person=None, phone=None, email=None, address=None, city=None,
                 state=None, country=None, tax_id=None, credit_limit=None,
                 opening_balance=Decimal("0"), notes=None)
    kwargs.update(overrides)
    return _repo().create(world["org_id"], CustomerCreate(**kwargs),
                          created_by=world["user_id"])


def _stock_in(world, quantity=Decimal("100")):
    SqlInventoryRepository().stock_in(world["org_id"], world["product_id"],
                                      world["warehouse_id"], quantity, world["user_id"])


def _order(world, customer_id, quantity=Decimal("2"), unit_price=Decimal("15")):
    return SqlSalesOrderRepository().create(
        world["org_id"],
        SalesOrderCreate(customer_id=customer_id, warehouse_id=world["warehouse_id"],
                         items=[SalesOrderItemInput(product_id=world["product_id"],
                                                    quantity_ordered=quantity,
                                                    unit_price=unit_price,
                                                    tax_percent=Decimal("0"))]),
        world["user_id"])


# -- create / update ------------------------------------------------------#

def test_create_stores_every_field(world):
    customer = _create_customer(world, customer_code="JANE-01", city="Kathmandu",
                                credit_limit=Decimal("500"), opening_balance=Decimal("100"))
    assert customer.customer_code == "JANE-01"
    assert customer.city == "Kathmandu"
    assert customer.credit_limit == Decimal("500")
    assert customer.opening_balance == Decimal("100")
    assert customer.created_by == world["user_id"]
    assert customer.is_active is True


def test_code_exists_true_for_existing_code(world):
    _create_customer(world, customer_code="JANE-01")
    assert _repo().code_exists(world["org_id"], "JANE-01") is True
    assert _repo().code_exists(world["org_id"], "NOBODY") is False


def test_code_exists_excludes_given_id(world):
    customer = _create_customer(world, customer_code="JANE-01")
    # excluding the row that itself owns the code means "is this code
    # available for ME to keep" reads as available, not a false collision
    assert _repo().code_exists(world["org_id"], "JANE-01", exclude_id=customer.id) is False


def test_update_applies_partial_fields(world):
    customer = _create_customer(world, name="Jane")
    updated = _repo().update(world["org_id"], customer.id,
                             CustomerUpdate(city="Pokhara", credit_limit=Decimal("1000")))
    assert updated.city == "Pokhara"
    assert updated.credit_limit == Decimal("1000")
    assert updated.name == "Jane"  # untouched fields survive a partial update


def test_duplicate_customer_code_violates_unique_constraint_and_rolls_back_cleanly(world):
    """The service layer pre-checks code_exists before ever calling
    create() (see SalesService.create_customer) — this proves the DB-level
    backstop actually works too, and that hitting it doesn't corrupt
    anything: no partial row is left behind, and the repository is still
    fully usable immediately afterward.
    """
    _create_customer(world, name="First", customer_code="DUP-CODE")
    before_count = len(_repo().list_all(world["org_id"]))

    with pytest.raises(IntegrityError):
        _repo().create(world["org_id"],
                       CustomerCreate(name="Second", customer_code="DUP-CODE",
                                     opening_balance=Decimal("0")),
                       created_by=world["user_id"])

    after_count = len(_repo().list_all(world["org_id"]))
    assert after_count == before_count  # the failed insert left nothing behind

    # the repository (and its underlying session factory) still work —
    # rollback didn't leave the connection/transaction in a broken state
    third = _create_customer(world, name="Third", customer_code="THIRD-CODE")
    assert third.customer_code == "THIRD-CODE"


# -- balance: opening balance, pending orders, invoices, payments --------#

def test_get_balance_starts_at_opening_balance_with_no_orders(world):
    customer = _create_customer(world, opening_balance=Decimal("250"),
                                credit_limit=Decimal("1000"))
    balance = _repo().get_balance(world["org_id"], customer.id)
    assert balance.opening_balance == Decimal("250")
    assert balance.invoiced_total == Decimal("0")
    assert balance.pending_orders_total == Decimal("0")
    assert balance.paid_total == Decimal("0")
    assert balance.outstanding_balance == Decimal("250")
    assert balance.available_credit == Decimal("750")


def test_get_balance_includes_uninvoiced_draft_order(world):
    customer = _create_customer(world)
    so = _order(world, customer.id, quantity=Decimal("2"), unit_price=Decimal("15"))  # = 30

    balance = _repo().get_balance(world["org_id"], customer.id)
    assert balance.pending_orders_total == Decimal("30")
    assert balance.invoiced_total == Decimal("0")
    assert balance.outstanding_balance == Decimal("30")


def test_get_balance_excludes_cancelled_order(world):
    customer = _create_customer(world)
    so = _order(world, customer.id, quantity=Decimal("2"), unit_price=Decimal("15"))
    SqlSalesOrderRepository().cancel(world["org_id"], so.id, world["user_id"])

    balance = _repo().get_balance(world["org_id"], customer.id)
    assert balance.pending_orders_total == Decimal("0")
    assert balance.outstanding_balance == Decimal("0")


def test_get_balance_moves_from_pending_to_invoiced_on_fulfillment(world):
    _stock_in(world)
    customer = _create_customer(world)
    sales_repo = SqlSalesOrderRepository()
    so = _order(world, customer.id, quantity=Decimal("2"), unit_price=Decimal("15"))  # = 30
    sales_repo.confirm(world["org_id"], so.id, world["user_id"])
    sales_repo.fulfill_sale(world["org_id"], so.id, world["user_id"])

    before_invoice = _repo().get_balance(world["org_id"], customer.id)
    assert before_invoice.pending_orders_total == Decimal("30")
    assert before_invoice.invoiced_total == Decimal("0")

    sales_repo.generate_invoice(world["org_id"], so.id, world["user_id"])

    after_invoice = _repo().get_balance(world["org_id"], customer.id)
    assert after_invoice.pending_orders_total == Decimal("0")
    assert after_invoice.invoiced_total == Decimal("30")
    assert after_invoice.outstanding_balance == Decimal("30")


def test_get_balance_reflects_payments_against_invoice(world):
    _stock_in(world)
    customer = _create_customer(world)
    sales_repo = SqlSalesOrderRepository()
    so = _order(world, customer.id, quantity=Decimal("2"), unit_price=Decimal("15"))  # = 30
    sales_repo.confirm(world["org_id"], so.id, world["user_id"])
    sales_repo.fulfill_sale(world["org_id"], so.id, world["user_id"])
    invoice = sales_repo.generate_invoice(world["org_id"], so.id, world["user_id"])

    sales_repo.record_payment(
        world["org_id"], PaymentRequest(invoice_id=invoice.id, amount=Decimal("10"),
                                        method="CASH"), world["user_id"])
    partial = _repo().get_balance(world["org_id"], customer.id)
    assert partial.paid_total == Decimal("10")
    assert partial.outstanding_balance == Decimal("20")

    sales_repo.record_payment(
        world["org_id"], PaymentRequest(invoice_id=invoice.id, amount=Decimal("20"),
                                        method="CASH"), world["user_id"])
    full = _repo().get_balance(world["org_id"], customer.id)
    assert full.paid_total == Decimal("30")
    assert full.outstanding_balance == Decimal("0")
    assert full.available_credit is None  # no credit_limit set on this customer


def test_get_history_includes_order_invoice_and_payment(world):
    _stock_in(world)
    customer = _create_customer(world)
    sales_repo = SqlSalesOrderRepository()
    so = _order(world, customer.id)
    sales_repo.confirm(world["org_id"], so.id, world["user_id"])
    sales_repo.fulfill_sale(world["org_id"], so.id, world["user_id"])
    invoice = sales_repo.generate_invoice(world["org_id"], so.id, world["user_id"])
    sales_repo.record_payment(
        world["org_id"], PaymentRequest(invoice_id=invoice.id, amount=Decimal("30"),
                                        method="CASH"), world["user_id"])

    history = _repo().get_history(world["org_id"], customer.id)
    kinds = {h.kind for h in history}
    assert kinds == {"sales_order", "invoice", "payment"}
    invoice_row = next(h for h in history if h.kind == "invoice")
    assert invoice_row.amount == Decimal("30")
    payment_row = next(h for h in history if h.kind == "payment")
    assert payment_row.amount == Decimal("30")


def test_get_history_includes_return(world):
    _stock_in(world)
    customer = _create_customer(world)
    sales_repo = SqlSalesOrderRepository()
    so = _order(world, customer.id, quantity=Decimal("5"))
    sales_repo.confirm(world["org_id"], so.id, world["user_id"])
    fulfilled = sales_repo.fulfill_sale(world["org_id"], so.id, world["user_id"])
    item_id = fulfilled.items[0].id

    sales_repo.record_return(world["org_id"], so.id, item_id, Decimal("1"), "damaged",
                             world["user_id"])

    history = _repo().get_history(world["org_id"], customer.id)
    return_row = next(h for h in history if h.kind == "return")
    assert return_row.reference == "damaged"

    # a return puts stock back but doesn't itself create a monetary
    # refund record (there's no credit-note/refund concept in this
    # system yet) — get_balance is unaffected by it, same as the invoice
    # total staying frozen once generated.
    balance = _repo().get_balance(world["org_id"], customer.id)
    assert balance.pending_orders_total == Decimal("75")  # 5 * 15, unchanged by the return
