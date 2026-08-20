"""SqlSalesOrderRepository (and SqlCustomerRepository) against a live
PostgreSQL database — proves the full sales workflow end to end: creating
an order never touches inventory, fulfillment atomically validates and
deducts stock + writes the ledger + advances status + writes an audit
entry, invoice numbering is unique even under concurrent generation,
payments accumulate and auto-complete the order once fully paid, and sales
returns put stock back and are validated against what was actually
fulfilled.

Uses the ``live_db`` fixture (tests/conftest.py) — see its docstring for
why this is gated on INVENTORY_TEST_DATABASE_URL, separate from the app's
real INVENTORY_DATABASE_URL, and how to run these locally.
"""
import threading
from decimal import Decimal

import pytest

from app.core.exceptions import OverpaymentError
from app.database.session import get_session
from app.domain.sales import PaymentMethod, SalesOrderStatus
from app.models import AuditLog, Customer, Inventory, Organization, Product, Unit, User, Warehouse
from app.repositories.sql.customer_repository import SqlCustomerRepository
from app.repositories.sql.inventory_repository import SqlInventoryRepository
from app.repositories.sql.sales_repository import SqlSalesOrderRepository
from app.schemas.sales import (
    CustomerCreate,
    PaymentRequest,
    SalesOrderCreate,
    SalesOrderItemInput,
    SalesOrderUpdate,
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
                          tax_percent=Decimal("13"), minimum_stock_level=Decimal("0"))
        product2 = Product(organization_id=org.id, sku="SKU-2", name="Gadget", unit_id=unit.id,
                           purchase_price=Decimal("20"), selling_price=Decimal("30"),
                           tax_percent=Decimal("13"), minimum_stock_level=Decimal("0"))
        warehouse = Warehouse(organization_id=org.id, code="MAIN", name="Main")
        customer = Customer(organization_id=org.id, name="Jane Buyer")
        user = User(email="clerk@example.com", username="clerk", hashed_password="x", full_name="Clerk")
        session.add_all([product, product2, warehouse, customer, user])
        session.flush()
        return {
            "org_id": org.id, "product_id": product.id, "product2_id": product2.id,
            "warehouse_id": warehouse.id, "customer_id": customer.id, "user_id": user.id,
        }


def _repo():
    return SqlSalesOrderRepository()


def _stock_in(world, quantity=Decimal("100")):
    SqlInventoryRepository().stock_in(world["org_id"], world["product_id"],
                                      world["warehouse_id"], quantity, world["user_id"])


def _create_so(world, quantity=Decimal("10"), unit_price=Decimal("15"),
              tax_percent=Decimal("13"), discount_percent=Decimal("0")):
    repo = _repo()
    data = SalesOrderCreate(
        customer_id=world["customer_id"], warehouse_id=world["warehouse_id"],
        items=[SalesOrderItemInput(product_id=world["product_id"], quantity_ordered=quantity,
                                   unit_price=unit_price, tax_percent=tax_percent,
                                   discount_percent=discount_percent)])
    return repo.create(world["org_id"], data, world["user_id"])


def _confirmed_so(world, quantity=Decimal("10"), unit_price=Decimal("15"),
                  tax_percent=Decimal("13"), discount_percent=Decimal("0")):
    repo = _repo()
    so = _create_so(world, quantity=quantity, unit_price=unit_price, tax_percent=tax_percent,
                    discount_percent=discount_percent)
    return repo.confirm(world["org_id"], so.id, world["user_id"])


def _inventory_on_hand(world) -> Decimal:
    level = SqlInventoryRepository().get_level(world["org_id"], world["product_id"],
                                               world["warehouse_id"])
    return level.quantity_on_hand


# -- create / edit --------------------------------------------------------#

def test_create_sales_order_starts_as_draft_and_does_not_touch_inventory(world):
    so = _create_so(world)
    assert so.status == SalesOrderStatus.DRAFT
    assert so.items[0].quantity_fulfilled == Decimal("0")

    with get_session() as session:
        assert session.query(Inventory).count() == 0


def test_update_replaces_items_wholesale(world):
    repo = _repo()
    so = _create_so(world, quantity=Decimal("5"))
    updated = repo.update(world["org_id"], so.id, SalesOrderUpdate(
        items=[SalesOrderItemInput(product_id=world["product2_id"], quantity_ordered=Decimal("3"),
                                   unit_price=Decimal("30"), tax_percent=Decimal("0"))]))
    assert len(updated.items) == 1
    assert updated.items[0].product_id == world["product2_id"]
    assert updated.items[0].quantity_ordered == Decimal("3")


def test_update_preserves_line_discount_percent(world):
    """Regression test: update() used to omit discount_percent when
    rebuilding items, silently resetting every line's discount to 0 —
    unlike create(), which always persisted it. See SalesOrderItem
    construction in SqlSalesOrderRepository.update.
    """
    repo = _repo()
    so = _create_so(world, quantity=Decimal("5"))
    updated = repo.update(world["org_id"], so.id, SalesOrderUpdate(
        items=[SalesOrderItemInput(product_id=world["product2_id"], quantity_ordered=Decimal("3"),
                                   unit_price=Decimal("30"), tax_percent=Decimal("0"),
                                   discount_percent=Decimal("10"))]))
    assert updated.items[0].discount_percent == Decimal("10")


def test_concurrent_update_and_confirm_cannot_both_apply_to_the_same_draft(world):
    """Regression test: update() used to load the SalesOrder via a plain
    (unlocked) db.get() and never re-checked status == DRAFT, so a
    confirm() racing in another thread could land between the service
    layer's own (also unlocked) DRAFT check and this transaction — letting
    items be deleted/replaced on an order that is no longer DRAFT. Now
    update()/confirm() both lock the row and re-validate under that lock,
    so exactly one of two orderings is possible: update fully completes
    before confirm reads status (both succeed, confirm operates on the new
    items), or confirm wins the race and update is rejected outright
    (SalesOrderValidationError) rather than silently applying anyway.
    """
    so = _create_so(world, quantity=Decimal("5"))
    repo = _repo()

    results = {}
    barrier = threading.Barrier(2)

    def do_update():
        barrier.wait()
        try:
            repo.update(world["org_id"], so.id, SalesOrderUpdate(
                items=[SalesOrderItemInput(product_id=world["product2_id"],
                                          quantity_ordered=Decimal("9"),
                                          unit_price=Decimal("30"), tax_percent=Decimal("0"))]))
            results["update"] = "ok"
        except Exception:
            results["update"] = "rejected"

    def do_confirm():
        barrier.wait()
        try:
            repo.confirm(world["org_id"], so.id, world["user_id"])
            results["confirm"] = "ok"
        except Exception:
            results["confirm"] = "rejected"

    threads = [threading.Thread(target=do_update), threading.Thread(target=do_confirm)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    final = repo.get_by_id(world["org_id"], so.id)
    assert final.status == SalesOrderStatus.CONFIRMED, "confirm must win eventually either way"

    if results["update"] == "ok":
        # update fully applied before confirm observed the row: the
        # confirmed order must reflect the NEW items, not the original ones.
        assert len(final.items) == 1
        assert final.items[0].product_id == world["product2_id"]
        assert final.items[0].quantity_ordered == Decimal("9")
    else:
        # confirm won the race: update must have been cleanly rejected,
        # never silently applied to a no-longer-DRAFT order.
        assert final.items[0].product_id == world["product_id"]
        assert final.items[0].quantity_ordered == Decimal("5")


# -- status transitions ----------------------------------------------------#

def test_confirm_sets_confirmed_by_and_at(world):
    repo = _repo()
    so = _create_so(world)
    confirmed = repo.confirm(world["org_id"], so.id, world["user_id"])
    assert confirmed.status == SalesOrderStatus.CONFIRMED
    assert confirmed.confirmed_by == world["user_id"]
    assert confirmed.confirmed_at is not None


def test_cancel_from_draft(world):
    repo = _repo()
    so = _create_so(world)
    cancelled = repo.cancel(world["org_id"], so.id, world["user_id"])
    assert cancelled.status == SalesOrderStatus.CANCELLED


def test_cancel_records_audit_log_entry(world):
    repo = _repo()
    so = _create_so(world)
    repo.cancel(world["org_id"], so.id, world["user_id"])

    with get_session() as session:
        entries = (session.query(AuditLog)
                  .filter_by(action="sales_order.cancel", entity_id=so.id).all())
        assert len(entries) == 1
        assert entries[0].user_id == world["user_id"]
        assert entries[0].actor_email == "clerk@example.com"
        assert entries[0].changes["before"]["status"] == "DRAFT"
        assert entries[0].changes["after"]["status"] == "CANCELLED"


# -- fulfillment ------------------------------------------------------------#

def test_fulfill_deducts_inventory_and_marks_fulfilled(world):
    _stock_in(world, Decimal("50"))
    repo = _repo()
    so = _confirmed_so(world, quantity=Decimal("10"))

    fulfilled = repo.fulfill_sale(world["org_id"], so.id, world["user_id"])

    assert fulfilled.status == SalesOrderStatus.FULFILLED
    assert fulfilled.items[0].quantity_fulfilled == Decimal("10")
    assert _inventory_on_hand(world) == Decimal("40")


def test_fulfill_rejects_insufficient_stock_and_rolls_back(world):
    _stock_in(world, Decimal("5"))
    repo = _repo()
    so = _confirmed_so(world, quantity=Decimal("10"))

    with pytest.raises(Exception):  # InsufficientStockError, from _apply
        repo.fulfill_sale(world["org_id"], so.id, world["user_id"])

    unchanged = repo.get_by_id(world["org_id"], so.id)
    assert unchanged.status == SalesOrderStatus.CONFIRMED
    assert unchanged.items[0].quantity_fulfilled == Decimal("0")
    assert _inventory_on_hand(world) == Decimal("5")


def test_fulfill_blocked_when_it_would_breach_minimum_stock_and_policy_is_block_sale(world):
    from app.core.exceptions import LowStockBlockedError
    from app.domain.inventory import LowStockBehavior

    _stock_in(world, Decimal("10"))
    with get_session() as session:
        org = session.get(Organization, world["org_id"])
        org.low_stock_behavior = LowStockBehavior.BLOCK_SALE
        product = session.get(Product, world["product_id"])
        product.minimum_stock_level = Decimal("5")

    repo = _repo()
    so = _confirmed_so(world, quantity=Decimal("8"))  # 10 - 8 = 2, below minimum of 5

    with pytest.raises(LowStockBlockedError):
        repo.fulfill_sale(world["org_id"], so.id, world["user_id"])

    # Nothing committed from the blocked attempt.
    unchanged = repo.get_by_id(world["org_id"], so.id)
    assert unchanged.status == SalesOrderStatus.CONFIRMED
    assert _inventory_on_hand(world) == Decimal("10")


def test_fulfill_allowed_below_minimum_when_policy_is_warn_only(world):
    # WARN_ONLY is the default — fulfilling below minimum_stock_level must
    # still succeed; only BLOCK_SALE enforces the policy.
    _stock_in(world, Decimal("10"))
    with get_session() as session:
        product = session.get(Product, world["product_id"])
        product.minimum_stock_level = Decimal("5")

    repo = _repo()
    so = _confirmed_so(world, quantity=Decimal("8"))

    fulfilled = repo.fulfill_sale(world["org_id"], so.id, world["user_id"])
    assert fulfilled.status == SalesOrderStatus.FULFILLED
    assert _inventory_on_hand(world) == Decimal("2")


def test_fulfill_only_allowed_from_confirmed(world):
    repo = _repo()
    so = _create_so(world)  # still DRAFT
    with pytest.raises(Exception):  # InvalidSalesOrderTransitionError
        repo.fulfill_sale(world["org_id"], so.id, world["user_id"])


def test_fulfill_multi_line_order_deducts_each_product(world):
    _stock_in(world, Decimal("50"))
    with get_session() as session:
        inv2 = Inventory(organization_id=world["org_id"], product_id=world["product2_id"],
                         warehouse_id=world["warehouse_id"], quantity_on_hand=Decimal("50"))
        session.add(inv2)

    repo = _repo()
    data = SalesOrderCreate(
        customer_id=world["customer_id"], warehouse_id=world["warehouse_id"],
        items=[SalesOrderItemInput(product_id=world["product_id"], quantity_ordered=Decimal("4"),
                                   unit_price=Decimal("15"), tax_percent=Decimal("0")),
              SalesOrderItemInput(product_id=world["product2_id"], quantity_ordered=Decimal("6"),
                                 unit_price=Decimal("30"), tax_percent=Decimal("0"))])
    so = repo.create(world["org_id"], data, world["user_id"])
    so = repo.confirm(world["org_id"], so.id, world["user_id"])
    repo.fulfill_sale(world["org_id"], so.id, world["user_id"])

    inv_repo = SqlInventoryRepository()
    level1 = inv_repo.get_level(world["org_id"], world["product_id"], world["warehouse_id"])
    level2 = inv_repo.get_level(world["org_id"], world["product2_id"], world["warehouse_id"])
    assert level1.quantity_on_hand == Decimal("46")
    assert level2.quantity_on_hand == Decimal("44")


def test_fulfill_rolls_back_earlier_lines_when_a_later_line_has_insufficient_stock(world):
    """fulfill_sale loops over every item in one transaction (see the
    module docstring). Item 1 has plenty of stock and would succeed on its
    own; item 2 doesn't. Proves the whole fulfillment is atomic — item 1's
    already-applied deduction must not survive just because it happened to
    be processed before the item that failed.
    """
    _stock_in(world, Decimal("50"))  # plenty for product 1
    with get_session() as session:
        inv2 = Inventory(organization_id=world["org_id"], product_id=world["product2_id"],
                         warehouse_id=world["warehouse_id"], quantity_on_hand=Decimal("2"))
        session.add(inv2)  # NOT enough for product 2's order

    repo = _repo()
    data = SalesOrderCreate(
        customer_id=world["customer_id"], warehouse_id=world["warehouse_id"],
        items=[SalesOrderItemInput(product_id=world["product_id"], quantity_ordered=Decimal("4"),
                                   unit_price=Decimal("15"), tax_percent=Decimal("0")),
              SalesOrderItemInput(product_id=world["product2_id"], quantity_ordered=Decimal("6"),
                                 unit_price=Decimal("30"), tax_percent=Decimal("0"))])
    so = repo.create(world["org_id"], data, world["user_id"])
    so = repo.confirm(world["org_id"], so.id, world["user_id"])

    with pytest.raises(Exception):  # InsufficientStockError, from _apply on product 2
        repo.fulfill_sale(world["org_id"], so.id, world["user_id"])

    inv_repo = SqlInventoryRepository()
    level1 = inv_repo.get_level(world["org_id"], world["product_id"], world["warehouse_id"])
    level2 = inv_repo.get_level(world["org_id"], world["product2_id"], world["warehouse_id"])
    assert level1.quantity_on_hand == Decimal("50")  # product 1's deduction did NOT persist
    assert level2.quantity_on_hand == Decimal("2")

    unchanged = repo.get_by_id(world["org_id"], so.id)
    assert unchanged.status == SalesOrderStatus.CONFIRMED  # never advanced to FULFILLED
    assert all(i.quantity_fulfilled == Decimal("0") for i in unchanged.items)


def test_fulfill_records_audit_log_entry(world):
    _stock_in(world, Decimal("50"))
    repo = _repo()
    so = _confirmed_so(world, quantity=Decimal("5"))
    repo.fulfill_sale(world["org_id"], so.id, world["user_id"])

    with get_session() as session:
        entries = (session.query(AuditLog)
                  .filter_by(action="sales_order.fulfill", entity_id=so.id).all())
        assert len(entries) == 1
        assert entries[0].actor_email == "clerk@example.com"


# -- invoicing ---------------------------------------------------------- #

def _fulfilled_so(world, quantity=Decimal("10"), unit_price=Decimal("15"),
                  tax_percent=Decimal("13"), stock=Decimal("100"),
                  discount_percent=Decimal("0")):
    _stock_in(world, stock)
    repo = _repo()
    so = _confirmed_so(world, quantity=quantity, unit_price=unit_price,
                       tax_percent=tax_percent, discount_percent=discount_percent)
    return repo.fulfill_sale(world["org_id"], so.id, world["user_id"])


def test_generate_invoice_computes_totals_from_items(world):
    repo = _repo()
    so = _fulfilled_so(world, quantity=Decimal("10"), unit_price=Decimal("100"),
                       tax_percent=Decimal("10"))

    invoice = repo.generate_invoice(world["org_id"], so.id, world["user_id"])

    assert invoice.subtotal == Decimal("1000")
    assert invoice.tax_amount == Decimal("100.00")
    assert invoice.total_amount == Decimal("1100.00")
    assert invoice.invoice_number.startswith("INV-")


def test_generate_invoice_applies_discount_before_tax(world):
    repo = _repo()
    # 10 x 100 = 1000 list price, 10% discount -> 900 taxable base,
    # 10% tax on 900 -> 90, total 990. Tax must NOT be computed on the
    # undiscounted 1000 (which would give 100 tax / 1100 total).
    so = _fulfilled_so(world, quantity=Decimal("10"), unit_price=Decimal("100"),
                       tax_percent=Decimal("10"), discount_percent=Decimal("10"))

    invoice = repo.generate_invoice(world["org_id"], so.id, world["user_id"])

    assert invoice.subtotal == Decimal("1000")
    assert invoice.discount_amount == Decimal("100.00")
    assert invoice.tax_amount == Decimal("90.00")
    assert invoice.total_amount == Decimal("990.00")


def test_get_invoice_document_assembles_company_customer_and_line_data(world):
    repo = _repo()
    with get_session() as session:
        org = session.get(Organization, world["org_id"])
        org.legal_name = "Acme Retail Pvt. Ltd."
        org.address = "1 Market St"
        org.phone = "+1-555-0100"

    so = _fulfilled_so(world, quantity=Decimal("4"), unit_price=Decimal("50"),
                       tax_percent=Decimal("13"), discount_percent=Decimal("10"))
    invoice = repo.generate_invoice(world["org_id"], so.id, world["user_id"])

    doc = repo.get_invoice_document(world["org_id"], invoice.id)

    assert doc.company_name == "Acme Retail"
    assert doc.company_legal_name == "Acme Retail Pvt. Ltd."
    assert doc.company_address == "1 Market St"
    assert doc.company_phone == "+1-555-0100"
    assert doc.customer_name == "Jane Buyer"
    assert doc.invoice_number == invoice.invoice_number
    assert len(doc.items) == 1
    line = doc.items[0]
    assert line.sku == "SKU-1"
    assert line.quantity == Decimal("4")
    assert line.line_subtotal == Decimal("200")
    assert line.line_discount == Decimal("20.00")
    assert line.line_total == invoice.total_amount


def test_get_invoice_document_payment_status_unpaid_when_nothing_paid(world):
    repo = _repo()
    so = _fulfilled_so(world, quantity=Decimal("1"), unit_price=Decimal("100"))
    invoice = repo.generate_invoice(world["org_id"], so.id, world["user_id"])

    doc = repo.get_invoice_document(world["org_id"], invoice.id)

    assert doc.amount_paid == Decimal("0")
    assert doc.amount_due == invoice.total_amount
    assert doc.payment_status.value == "UNPAID"


def test_get_invoice_document_payment_status_partially_paid(world):
    from app.schemas.sales import PaymentRequest
    from app.domain.sales import PaymentMethod

    repo = _repo()
    so = _fulfilled_so(world, quantity=Decimal("1"), unit_price=Decimal("100"),
                       tax_percent=Decimal("0"))
    invoice = repo.generate_invoice(world["org_id"], so.id, world["user_id"])
    repo.record_payment(world["org_id"], PaymentRequest(
        invoice_id=invoice.id, amount=Decimal("40"), method=PaymentMethod.CASH),
        world["user_id"])

    doc = repo.get_invoice_document(world["org_id"], invoice.id)

    assert doc.amount_paid == Decimal("40")
    assert doc.amount_due == Decimal("60")
    assert doc.payment_status.value == "PARTIALLY_PAID"


def test_get_invoice_document_payment_status_paid(world):
    from app.schemas.sales import PaymentRequest
    from app.domain.sales import PaymentMethod

    repo = _repo()
    so = _fulfilled_so(world, quantity=Decimal("1"), unit_price=Decimal("100"),
                       tax_percent=Decimal("0"))
    invoice = repo.generate_invoice(world["org_id"], so.id, world["user_id"])
    repo.record_payment(world["org_id"], PaymentRequest(
        invoice_id=invoice.id, amount=Decimal("100"), method=PaymentMethod.CASH),
        world["user_id"])

    doc = repo.get_invoice_document(world["org_id"], invoice.id)

    assert doc.amount_due == Decimal("0")
    assert doc.payment_status.value == "PAID"


def test_get_invoice_by_sales_order_returns_the_generated_invoice(world):
    repo = _repo()
    so = _fulfilled_so(world, quantity=Decimal("1"), unit_price=Decimal("100"))
    invoice = repo.generate_invoice(world["org_id"], so.id, world["user_id"])

    found = repo.get_invoice_by_sales_order(world["org_id"], so.id)

    assert found is not None
    assert found.id == invoice.id
    assert found.invoice_number == invoice.invoice_number


def test_get_invoice_by_sales_order_none_when_not_yet_generated(world):
    repo = _repo()
    so = _confirmed_so(world)   # not fulfilled/invoiced yet
    assert repo.get_invoice_by_sales_order(world["org_id"], so.id) is None


def test_get_invoice_document_missing_returns_none(world):
    import uuid
    repo = _repo()
    assert repo.get_invoice_document(world["org_id"], uuid.uuid4()) is None


def test_generate_invoice_requires_fulfilled_status(world):
    repo = _repo()
    so = _confirmed_so(world)  # not yet fulfilled
    with pytest.raises(Exception):  # SalesOrderValidationError
        repo.generate_invoice(world["org_id"], so.id, world["user_id"])


def test_generate_invoice_rejects_duplicate(world):
    repo = _repo()
    so = _fulfilled_so(world)
    repo.generate_invoice(world["org_id"], so.id, world["user_id"])
    with pytest.raises(Exception):  # DuplicateInvoiceError
        repo.generate_invoice(world["org_id"], so.id, world["user_id"])


def test_invoice_numbers_increment_sequentially_per_organization(world):
    repo = _repo()
    numbers = []
    for _ in range(3):
        so = _fulfilled_so(world, quantity=Decimal("1"))
        invoice = repo.generate_invoice(world["org_id"], so.id, world["user_id"])
        numbers.append(invoice.invoice_number)
    assert numbers == ["INV-000001", "INV-000002", "INV-000003"]


def test_invoice_number_prefix_is_configurable(world):
    with get_session() as session:
        org = session.get(Organization, world["org_id"])
        org.invoice_number_prefix = "ACME-"

    repo = _repo()
    so = _fulfilled_so(world)
    invoice = repo.generate_invoice(world["org_id"], so.id, world["user_id"])
    assert invoice.invoice_number == "ACME-000001"


def test_concurrent_invoice_generation_produces_unique_numbers(world):
    """20 sales orders, fulfilled up front, then 20 threads each generate
    one invoice concurrently. The locked InvoiceSequence counter must
    serialize them: 20 invoices, 20 distinct numbers, no collisions.
    """
    repo = _repo()
    order_ids = []
    for _ in range(20):
        so = _fulfilled_so(world, quantity=Decimal("1"))
        order_ids.append(so.id)

    results = []
    lock = threading.Lock()

    def generate(order_id):
        invoice = repo.generate_invoice(world["org_id"], order_id, world["user_id"])
        with lock:
            results.append(invoice.invoice_number)

    threads = [threading.Thread(target=generate, args=(oid,)) for oid in order_ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 20
    assert len(set(results)) == 20, "duplicate invoice numbers generated concurrently"


# -- payments -------------------------------------------------------------- #

def test_partial_payment_does_not_complete_order(world):
    repo = _repo()
    so = _fulfilled_so(world, quantity=Decimal("10"), unit_price=Decimal("100"),
                       tax_percent=Decimal("0"))
    invoice = repo.generate_invoice(world["org_id"], so.id, world["user_id"])

    repo.record_payment(world["org_id"],
                        PaymentRequest(invoice_id=invoice.id, amount=Decimal("400"),
                                      method=PaymentMethod.CASH), world["user_id"])

    updated = repo.get_by_id(world["org_id"], so.id)
    assert updated.status == SalesOrderStatus.FULFILLED


def test_full_payment_completes_order(world):
    repo = _repo()
    so = _fulfilled_so(world, quantity=Decimal("10"), unit_price=Decimal("100"),
                       tax_percent=Decimal("0"))
    invoice = repo.generate_invoice(world["org_id"], so.id, world["user_id"])

    repo.record_payment(world["org_id"],
                        PaymentRequest(invoice_id=invoice.id, amount=Decimal("1000"),
                                      method=PaymentMethod.CARD), world["user_id"])

    updated = repo.get_by_id(world["org_id"], so.id)
    assert updated.status == SalesOrderStatus.COMPLETED


def test_payment_in_installments_completes_order_once_total_reached(world):
    repo = _repo()
    so = _fulfilled_so(world, quantity=Decimal("10"), unit_price=Decimal("100"),
                       tax_percent=Decimal("0"))
    invoice = repo.generate_invoice(world["org_id"], so.id, world["user_id"])

    repo.record_payment(world["org_id"],
                        PaymentRequest(invoice_id=invoice.id, amount=Decimal("600"),
                                      method=PaymentMethod.CASH), world["user_id"])
    assert repo.get_by_id(world["org_id"], so.id).status == SalesOrderStatus.FULFILLED

    repo.record_payment(world["org_id"],
                        PaymentRequest(invoice_id=invoice.id, amount=Decimal("400"),
                                      method=PaymentMethod.CASH), world["user_id"])
    assert repo.get_by_id(world["org_id"], so.id).status == SalesOrderStatus.COMPLETED


def test_payment_amount_is_decimal_not_float(world):
    repo = _repo()
    so = _fulfilled_so(world, quantity=Decimal("3"), unit_price=Decimal("9.99"),
                       tax_percent=Decimal("0"))
    invoice = repo.generate_invoice(world["org_id"], so.id, world["user_id"])
    assert isinstance(invoice.total_amount, Decimal)
    assert invoice.total_amount == Decimal("29.97")

    payment = repo.record_payment(world["org_id"],
                                  PaymentRequest(invoice_id=invoice.id,
                                                amount=Decimal("29.97"),
                                                method=PaymentMethod.CASH),
                                  world["user_id"])
    assert isinstance(payment.amount, Decimal)


def test_single_payment_exceeding_invoice_total_is_rejected(world):
    repo = _repo()
    so = _fulfilled_so(world, quantity=Decimal("10"), unit_price=Decimal("100"),
                       tax_percent=Decimal("0"))
    invoice = repo.generate_invoice(world["org_id"], so.id, world["user_id"])

    with pytest.raises(OverpaymentError):
        repo.record_payment(world["org_id"],
                           PaymentRequest(invoice_id=invoice.id, amount=Decimal("1000.01"),
                                         method=PaymentMethod.CASH), world["user_id"])

    # Nothing was recorded — the invoice's outstanding balance is untouched.
    doc = repo.get_invoice_document(world["org_id"], invoice.id)
    assert doc.amount_paid == Decimal("0")


def test_installment_payment_exceeding_remaining_balance_is_rejected(world):
    repo = _repo()
    so = _fulfilled_so(world, quantity=Decimal("10"), unit_price=Decimal("100"),
                       tax_percent=Decimal("0"))
    invoice = repo.generate_invoice(world["org_id"], so.id, world["user_id"])

    repo.record_payment(world["org_id"],
                        PaymentRequest(invoice_id=invoice.id, amount=Decimal("600"),
                                      method=PaymentMethod.CASH), world["user_id"])

    # Only 400 remains outstanding — 400.01 must be rejected even though
    # it's well under the invoice's original total of 1000.
    with pytest.raises(OverpaymentError):
        repo.record_payment(world["org_id"],
                           PaymentRequest(invoice_id=invoice.id, amount=Decimal("400.01"),
                                         method=PaymentMethod.CASH), world["user_id"])

    assert repo.get_by_id(world["org_id"], so.id).status == SalesOrderStatus.FULFILLED


def test_payment_of_exactly_the_remaining_balance_is_accepted(world):
    repo = _repo()
    so = _fulfilled_so(world, quantity=Decimal("10"), unit_price=Decimal("100"),
                       tax_percent=Decimal("0"))
    invoice = repo.generate_invoice(world["org_id"], so.id, world["user_id"])
    repo.record_payment(world["org_id"],
                        PaymentRequest(invoice_id=invoice.id, amount=Decimal("600"),
                                      method=PaymentMethod.CASH), world["user_id"])

    repo.record_payment(world["org_id"],
                        PaymentRequest(invoice_id=invoice.id, amount=Decimal("400"),
                                      method=PaymentMethod.CASH), world["user_id"])

    assert repo.get_by_id(world["org_id"], so.id).status == SalesOrderStatus.COMPLETED


def test_concurrent_payments_cannot_jointly_overpay_an_invoice(world):
    # Two threads each try to pay 700 against a 1000 invoice at the same
    # moment (1400 combined > 1000). The Invoice-row lock in
    # record_payment must serialize them: the second to acquire the lock
    # sees the first payment's effect and gets rejected as an
    # overpayment, rather than both succeeding.
    repo = _repo()
    so = _fulfilled_so(world, quantity=Decimal("10"), unit_price=Decimal("100"),
                       tax_percent=Decimal("0"))
    invoice = repo.generate_invoice(world["org_id"], so.id, world["user_id"])

    results = []
    barrier = threading.Barrier(2)

    def attempt():
        barrier.wait()
        try:
            repo.record_payment(
                world["org_id"],
                PaymentRequest(invoice_id=invoice.id, amount=Decimal("700"),
                              method=PaymentMethod.CASH), world["user_id"])
            results.append("ok")
        except OverpaymentError:
            results.append("rejected")

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == ["ok", "rejected"]
    doc = repo.get_invoice_document(world["org_id"], invoice.id)
    assert doc.amount_paid == Decimal("700")  # exactly one payment landed


def test_concurrent_order_creation_cannot_jointly_exceed_credit_limit(world):
    # Customer has credit_limit=1000, no prior exposure (available=1000).
    # Two threads each try to create an order totaling 800 at the same
    # moment (1600 combined > 1000). The Customer-row lock in create() must
    # serialize them: the second to acquire the lock sees the first order's
    # effect (via compute_customer_balance's pending_orders_total) and gets
    # rejected, rather than both succeeding — closes the TOCTOU race an
    # unlocked balance-then-insert would have.
    with get_session() as session:
        customer = session.get(Customer, world["customer_id"])
        customer.credit_limit = Decimal("1000")

    repo = _repo()
    results = []
    barrier = threading.Barrier(2)

    def attempt():
        barrier.wait()
        try:
            repo.create(world["org_id"], SalesOrderCreate(
                customer_id=world["customer_id"], warehouse_id=world["warehouse_id"],
                items=[SalesOrderItemInput(product_id=world["product_id"],
                                          quantity_ordered=Decimal("8"),
                                          unit_price=Decimal("100"),
                                          tax_percent=Decimal("0"),
                                          discount_percent=Decimal("0"))]),
                       world["user_id"])
            results.append("ok")
        except Exception:  # CreditLimitExceededError
            results.append("rejected")

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == ["ok", "rejected"]
    balance = SqlCustomerRepository().get_balance(world["org_id"], world["customer_id"])
    assert balance.outstanding_balance == Decimal("800.00")  # exactly one order landed


# -- sales returns ----------------------------------------------------------#

def test_record_return_puts_stock_back_and_reduces_net_fulfilled(world):
    repo = _repo()
    so = _fulfilled_so(world, quantity=Decimal("10"))
    on_hand_after_fulfil = _inventory_on_hand(world)

    result = repo.record_return(world["org_id"], so.id, so.items[0].id, Decimal("3"),
                                "wrong size", world["user_id"])

    assert result.quantity == Decimal("3")
    assert _inventory_on_hand(world) == on_hand_after_fulfil + Decimal("3")

    updated = repo.get_by_id(world["org_id"], so.id)
    assert updated.items[0].quantity_fulfilled == Decimal("7")


def test_record_return_rejects_more_than_fulfilled(world):
    repo = _repo()
    so = _fulfilled_so(world, quantity=Decimal("5"))

    with pytest.raises(Exception):  # SalesOrderValidationError
        repo.record_return(world["org_id"], so.id, so.items[0].id, Decimal("6"), "too many",
                          world["user_id"])

    unchanged = repo.get_by_id(world["org_id"], so.id)
    assert unchanged.items[0].quantity_fulfilled == Decimal("5")


def test_record_return_creates_return_in_ledger_entry(world):
    repo = _repo()
    so = _fulfilled_so(world, quantity=Decimal("10"))

    result = repo.record_return(world["org_id"], so.id, so.items[0].id, Decimal("2"),
                                "damaged", world["user_id"])

    from app.models import InventoryTransaction
    with get_session() as session:
        tx = session.get(InventoryTransaction, result.inventory_transaction_id)
        assert tx.transaction_type.value == "RETURN_IN"
        assert tx.quantity_change == Decimal("2")


def test_record_return_computes_credit_amount_from_the_original_line_pricing(world):
    repo = _repo()
    # unit_price 100, 13% tax, 10% discount -> per-unit credit = 100*0.9*1.13 = 101.70
    so = _fulfilled_so(world, quantity=Decimal("10"), unit_price=Decimal("100"),
                       tax_percent=Decimal("13"), discount_percent=Decimal("10"))

    result = repo.record_return(world["org_id"], so.id, so.items[0].id, Decimal("2"),
                                "wrong size", world["user_id"])

    assert result.credit_amount == Decimal("203.40")


def test_record_return_reduces_customer_outstanding_balance(world):
    """A return isn't just a stock movement — it must reduce what the
    customer owes, the same as a payment would (CRITICAL BUSINESS RULE:
    returns/refunds must correctly update inventory AND financial records).
    """
    repo = _repo()
    customer_repo = SqlCustomerRepository()
    so = _fulfilled_so(world, quantity=Decimal("10"), unit_price=Decimal("100"),
                       tax_percent=Decimal("0"))
    repo.generate_invoice(world["org_id"], so.id, world["user_id"])

    before = customer_repo.get_balance(world["org_id"], world["customer_id"])
    assert before.outstanding_balance == Decimal("1000.00")

    result = repo.record_return(world["org_id"], so.id, so.items[0].id, Decimal("3"),
                                "damaged", world["user_id"])
    assert result.credit_amount == Decimal("300.00")

    after = customer_repo.get_balance(world["org_id"], world["customer_id"])
    assert after.returns_credit_total == Decimal("300.00")
    assert after.outstanding_balance == Decimal("700.00")


def test_record_return_records_audit_log_entry(world):
    repo = _repo()
    so = _fulfilled_so(world, quantity=Decimal("10"))

    repo.record_return(world["org_id"], so.id, so.items[0].id, Decimal("2"), "damaged",
                       world["user_id"])

    with get_session() as session:
        entries = (session.query(AuditLog)
                  .filter_by(action="sales_order.record_return", entity_id=so.id).all())
        assert len(entries) == 1
        assert entries[0].user_id == world["user_id"]
        assert entries[0].actor_email == "clerk@example.com"
        assert entries[0].changes["quantity"] == "2"
        assert entries[0].changes["reason"] == "damaged"


# -- finalize_new_bill (New Bill: confirm+fulfill+invoice[+payment] atomically) --#

def test_finalize_new_bill_confirms_fulfills_and_invoices(world):
    from app.schemas.sales import FinalizeSaleRequest

    _stock_in(world, Decimal("50"))
    repo = _repo()
    so = _create_so(world, quantity=Decimal("10"), unit_price=Decimal("100"),
                    tax_percent=Decimal("10"))

    result = repo.finalize_new_bill(world["org_id"], so.id, world["user_id"],
                                    FinalizeSaleRequest())

    assert result.sales_order.status == SalesOrderStatus.FULFILLED
    assert result.sales_order.items[0].quantity_fulfilled == Decimal("10")
    assert result.invoice.subtotal == Decimal("1000")
    assert result.invoice.tax_amount == Decimal("100.00")
    assert result.invoice.total_amount == Decimal("1100.00")
    assert result.invoice.invoice_number.startswith("INV-")
    assert result.payment is None
    assert _inventory_on_hand(world) == Decimal("40")


def test_finalize_new_bill_records_payment_and_completes_order_when_fully_paid(world):
    from app.schemas.sales import FinalizeSaleRequest

    _stock_in(world, Decimal("50"))
    repo = _repo()
    so = _create_so(world, quantity=Decimal("10"), unit_price=Decimal("100"),
                    tax_percent=Decimal("0"))

    result = repo.finalize_new_bill(
        world["org_id"], so.id, world["user_id"],
        FinalizeSaleRequest(payment_amount=Decimal("1000"), payment_method=PaymentMethod.CASH))

    assert result.payment is not None
    assert result.payment.amount == Decimal("1000")
    assert result.payment.method == PaymentMethod.CASH
    assert result.sales_order.status == SalesOrderStatus.COMPLETED


def test_finalize_new_bill_persists_due_date_overall_discount_and_other_charges(world):
    import datetime as dt

    from app.schemas.sales import FinalizeSaleRequest

    _stock_in(world, Decimal("50"))
    repo = _repo()
    so = _create_so(world, quantity=Decimal("10"), unit_price=Decimal("100"),
                    tax_percent=Decimal("10"))
    due = dt.date(2026, 9, 1)

    result = repo.finalize_new_bill(
        world["org_id"], so.id, world["user_id"],
        FinalizeSaleRequest(due_date=due, overall_discount_amount=Decimal("50"),
                            other_charges=Decimal("20")))

    invoice = result.invoice
    assert invoice.due_date == due
    assert invoice.overall_discount_amount == Decimal("50")
    assert invoice.other_charges == Decimal("20")
    # subtotal 1000 - item_discount 0 - overall_discount 50 + tax 100 + other_charges 20 = 1070
    assert invoice.total_amount == Decimal("1070.00")


def test_finalize_new_bill_rolls_back_everything_on_insufficient_stock(world):
    from app.schemas.sales import FinalizeSaleRequest

    _stock_in(world, Decimal("5"))  # not enough for the order below
    repo = _repo()
    so = _create_so(world, quantity=Decimal("10"))

    with pytest.raises(Exception):  # InsufficientStockError, from _fulfill's _apply
        repo.finalize_new_bill(world["org_id"], so.id, world["user_id"], FinalizeSaleRequest())

    unchanged = repo.get_by_id(world["org_id"], so.id)
    assert unchanged.status == SalesOrderStatus.DRAFT
    assert unchanged.items[0].quantity_fulfilled == Decimal("0")
    assert _inventory_on_hand(world) == Decimal("5")
    assert repo.get_invoice_by_sales_order(world["org_id"], so.id) is None

    with get_session() as session:
        entries = (session.query(AuditLog)
                  .filter_by(action="sales_order.new_bill_finalized", entity_id=so.id).all())
        assert entries == []


def test_finalize_new_bill_rejects_non_draft_sales_order(world):
    from app.schemas.sales import FinalizeSaleRequest

    _stock_in(world, Decimal("50"))
    repo = _repo()
    so = _confirmed_so(world)  # already CONFIRMED, not DRAFT

    with pytest.raises(Exception):  # InvalidSalesOrderTransitionError
        repo.finalize_new_bill(world["org_id"], so.id, world["user_id"], FinalizeSaleRequest())


def test_finalize_new_bill_records_audit_log_entry(world):
    from app.schemas.sales import FinalizeSaleRequest

    _stock_in(world, Decimal("50"))
    repo = _repo()
    so = _create_so(world, quantity=Decimal("10"), unit_price=Decimal("100"),
                    tax_percent=Decimal("0"))

    result = repo.finalize_new_bill(
        world["org_id"], so.id, world["user_id"],
        FinalizeSaleRequest(payment_amount=Decimal("400"), payment_method=PaymentMethod.CASH))

    with get_session() as session:
        entries = (session.query(AuditLog)
                  .filter_by(action="sales_order.new_bill_finalized", entity_id=so.id).all())
        assert len(entries) == 1
        assert entries[0].actor_email == "clerk@example.com"
        assert entries[0].changes["invoice_number"] == result.invoice.invoice_number
        assert entries[0].changes["payment_amount"] == "400"


def test_finalize_new_bill_invoice_numbers_stay_unique_across_bills(world):
    from app.schemas.sales import FinalizeSaleRequest

    _stock_in(world, Decimal("50"))
    repo = _repo()
    numbers = []
    for _ in range(3):
        so = _create_so(world, quantity=Decimal("1"))
        result = repo.finalize_new_bill(world["org_id"], so.id, world["user_id"],
                                        FinalizeSaleRequest())
        numbers.append(result.invoice.invoice_number)
    assert len(set(numbers)) == 3


# -- customer repository -----------------------------------------------------#

def test_customer_repository_scopes_by_organization(world):
    with get_session() as session:
        other_org = Organization(name="Other Org")
        session.add(other_org)
        session.flush()
        other_org_id = other_org.id

    repo = SqlCustomerRepository()
    created = repo.create(world["org_id"], CustomerCreate(name="New Customer"))
    assert repo.get_by_id(other_org_id, created.id) is None
    assert repo.get_by_id(world["org_id"], created.id) is not None
