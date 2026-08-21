"""SqlPurchaseOrderRepository (and SqlSupplierRepository) against a live
PostgreSQL database — proves the full purchasing workflow end to end：
creating a PO never touches inventory, receiving goods atomically updates
inventory + the ledger + PO status + the audit trail, partial receiving
across multiple receipts, purchase returns, and that a failed receipt line
rolls back everything else in the same receipt.

Uses the ``live_db`` fixture (tests/conftest.py) — see its docstring for
why this is gated on INVENTORY_TEST_DATABASE_URL, separate from the app's
real INVENTORY_DATABASE_URL, and how to run these locally.
"""
import threading
from decimal import Decimal

import pytest

from app.database.session import get_session
from app.domain.purchasing import PurchaseOrderStatus
from app.models import (
    AuditLog,
    GoodsReceipt,
    Inventory,
    Organization,
    Product,
    Supplier,
    Unit,
    User,
    Warehouse,
)
from app.repositories.sql.inventory_repository import SqlInventoryRepository
from app.repositories.sql.purchase_repository import SqlPurchaseOrderRepository
from app.repositories.sql.supplier_repository import SqlSupplierRepository
from app.schemas.purchasing import (
    GoodsReceiptLineInput,
    PurchaseOrderCreate,
    PurchaseOrderItemInput,
    PurchaseOrderUpdate,
    SupplierCreate,
)


@pytest.fixture()
def world(live_db):
    with get_session() as session:
        org = Organization(name="Acme Traders")
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
        supplier = Supplier(organization_id=org.id, name="Best Supplies")
        user = User(email="buyer@example.com", username="buyer", hashed_password="x",
                   full_name="Buyer")
        session.add_all([product, product2, warehouse, supplier, user])
        session.flush()
        return {
            "org_id": org.id, "product_id": product.id, "product2_id": product2.id,
            "warehouse_id": warehouse.id, "supplier_id": supplier.id, "user_id": user.id,
        }


def _repo():
    return SqlPurchaseOrderRepository()


def _create_po(world, quantity=Decimal("100"), unit_price=Decimal("10"),
              tax_percent=Decimal("13"), custom_fields=None):
    repo = _repo()
    data = PurchaseOrderCreate(
        supplier_id=world["supplier_id"], warehouse_id=world["warehouse_id"],
        custom_fields=custom_fields,
        items=[PurchaseOrderItemInput(product_id=world["product_id"],
                                      quantity_ordered=quantity, unit_price=unit_price,
                                      tax_percent=tax_percent)])
    return repo.create(world["org_id"], data, world["user_id"])


def _inventory_on_hand(world) -> Decimal:
    inv_repo = SqlInventoryRepository()
    level = inv_repo.get_level(world["org_id"], world["product_id"], world["warehouse_id"])
    return level.quantity_on_hand


# -- create / edit --------------------------------------------------------#

def test_create_purchase_order_starts_as_draft_and_does_not_touch_inventory(world):
    po = _create_po(world)
    assert po.status == PurchaseOrderStatus.DRAFT
    assert len(po.items) == 1
    assert po.items[0].quantity_received == Decimal("0")
    assert _inventory_on_hand(world) == Decimal("0")

    with get_session() as session:
        assert session.query(Inventory).count() == 0


def test_update_replaces_items_wholesale(world):
    repo = _repo()
    po = _create_po(world, quantity=Decimal("10"))
    updated = repo.update(world["org_id"], po.id, PurchaseOrderUpdate(
        items=[PurchaseOrderItemInput(product_id=world["product2_id"],
                                      quantity_ordered=Decimal("5"),
                                      unit_price=Decimal("20"), tax_percent=Decimal("0"))]))
    assert len(updated.items) == 1
    assert updated.items[0].product_id == world["product2_id"]
    assert updated.items[0].quantity_ordered == Decimal("5")


def test_create_persists_custom_fields(world):
    po = _create_po(world, custom_fields={"dock": "A1"})
    fetched = _repo().get_by_id(world["org_id"], po.id)
    assert fetched.custom_fields == {"dock": "A1"}


def test_create_assigns_sequential_order_numbers_with_configured_prefix(world):
    repo = _repo()
    with get_session() as session:
        org = session.get(Organization, world["org_id"])
        org.purchase_number_prefix = "PUR-"

    po1 = _create_po(world)
    po2 = _create_po(world)

    assert po1.order_number == "PUR-000001"
    assert po2.order_number == "PUR-000002"


# -- status transitions ----------------------------------------------------#

def test_submit_then_approve(world):
    repo = _repo()
    po = _create_po(world)
    submitted = repo.submit(world["org_id"], po.id)
    assert submitted.status == PurchaseOrderStatus.SUBMITTED

    approved = repo.approve(world["org_id"], po.id, world["user_id"])
    assert approved.status == PurchaseOrderStatus.APPROVED
    assert approved.approved_by == world["user_id"]
    assert approved.approved_at is not None


def test_cancel_from_draft(world):
    repo = _repo()
    po = _create_po(world)
    cancelled = repo.cancel(world["org_id"], po.id, world["user_id"])
    assert cancelled.status == PurchaseOrderStatus.CANCELLED


def test_approve_records_audit_log_entry(world):
    repo = _repo()
    po = _create_po(world)
    repo.submit(world["org_id"], po.id)
    repo.approve(world["org_id"], po.id, world["user_id"])

    with get_session() as session:
        entries = (session.query(AuditLog)
                  .filter_by(action="purchase_order.approve", entity_id=po.id).all())
        assert len(entries) == 1
        assert entries[0].user_id == world["user_id"]
        assert entries[0].actor_email == "buyer@example.com"
        assert entries[0].changes["before"]["status"] == "SUBMITTED"
        assert entries[0].changes["after"]["status"] == "APPROVED"


def test_cancel_records_audit_log_entry(world):
    repo = _repo()
    po = _create_po(world)
    repo.cancel(world["org_id"], po.id, world["user_id"])

    with get_session() as session:
        entries = (session.query(AuditLog)
                  .filter_by(action="purchase_order.cancel", entity_id=po.id).all())
        assert len(entries) == 1
        assert entries[0].user_id == world["user_id"]
        assert entries[0].actor_email == "buyer@example.com"
        assert entries[0].changes["before"]["status"] == "DRAFT"
        assert entries[0].changes["after"]["status"] == "CANCELLED"


def test_concurrent_approve_and_cancel_cannot_both_apply(world):
    """Regression test: approve()/cancel() used to load the PurchaseOrder
    via a plain (unlocked) db.get() with no re-validation of the current
    status, so two concurrent transitions on the same SUBMITTED PO (e.g. a
    clerk approving it while a manager cancels it) could both blindly
    overwrite po.status with last-write-wins semantics, leaving no
    guarantee about which transition "actually" happened or an audit trail
    that doesn't match the final state. Now both lock the row and
    re-validate the transition under that lock: exactly one of the two
    concurrent calls must succeed, and the other must be rejected outright
    rather than silently clobbering the winner's status.
    """
    repo = _repo()
    po = _create_po(world)
    repo.submit(world["org_id"], po.id)

    results = {}
    barrier = threading.Barrier(2)

    def do_approve():
        barrier.wait()
        try:
            repo.approve(world["org_id"], po.id, world["user_id"])
            results["approve"] = "ok"
        except Exception:
            results["approve"] = "rejected"

    def do_cancel():
        barrier.wait()
        try:
            repo.cancel(world["org_id"], po.id, world["user_id"])
            results["cancel"] = "ok"
        except Exception:
            results["cancel"] = "rejected"

    threads = [threading.Thread(target=do_approve), threading.Thread(target=do_cancel)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Exactly one transition must have won — never both "ok" (that would
    # mean the loser's write silently clobbered the winner's).
    outcomes = sorted(results.values())
    assert outcomes == ["ok", "rejected"]

    final = repo.get_by_id(world["org_id"], po.id)
    if results["approve"] == "ok":
        assert final.status == PurchaseOrderStatus.APPROVED
    else:
        assert final.status == PurchaseOrderStatus.CANCELLED


# -- receiving goods -------------------------------------------------------#

def _approved_po(world, quantity=Decimal("100")):
    repo = _repo()
    po = _create_po(world, quantity=quantity)
    repo.submit(world["org_id"], po.id)
    return repo.approve(world["org_id"], po.id, world["user_id"])


def test_receive_full_quantity_marks_received_and_updates_inventory(world):
    repo = _repo()
    po = _approved_po(world, quantity=Decimal("50"))
    item_id = po.items[0].id

    receipt = repo.receive_goods(
        world["org_id"], po.id,
        [GoodsReceiptLineInput(purchase_order_item_id=item_id, quantity=Decimal("50"))],
        world["user_id"], notes="full delivery")

    assert len(receipt.items) == 1
    assert receipt.items[0].quantity_received == Decimal("50")

    updated_po = repo.get_by_id(world["org_id"], po.id)
    assert updated_po.status == PurchaseOrderStatus.RECEIVED
    assert updated_po.items[0].quantity_received == Decimal("50")
    assert _inventory_on_hand(world) == Decimal("50")


def test_partial_receiving_across_two_receipts(world):
    repo = _repo()
    po = _approved_po(world, quantity=Decimal("100"))
    item_id = po.items[0].id

    repo.receive_goods(world["org_id"], po.id,
                       [GoodsReceiptLineInput(purchase_order_item_id=item_id,
                                              quantity=Decimal("30"))],
                       world["user_id"])
    after_first = repo.get_by_id(world["org_id"], po.id)
    assert after_first.status == PurchaseOrderStatus.PARTIALLY_RECEIVED
    assert after_first.items[0].quantity_received == Decimal("30")
    assert _inventory_on_hand(world) == Decimal("30")

    repo.receive_goods(world["org_id"], po.id,
                       [GoodsReceiptLineInput(purchase_order_item_id=item_id,
                                              quantity=Decimal("70"))],
                       world["user_id"])
    after_second = repo.get_by_id(world["org_id"], po.id)
    assert after_second.status == PurchaseOrderStatus.RECEIVED
    assert after_second.items[0].quantity_received == Decimal("100")
    assert _inventory_on_hand(world) == Decimal("100")


def test_receive_goods_rejects_over_receipt(world):
    repo = _repo()
    po = _approved_po(world, quantity=Decimal("10"))
    item_id = po.items[0].id

    with pytest.raises(Exception):  # PurchaseOrderValidationError
        repo.receive_goods(world["org_id"], po.id,
                          [GoodsReceiptLineInput(purchase_order_item_id=item_id,
                                                 quantity=Decimal("11"))],
                          world["user_id"])

    unchanged = repo.get_by_id(world["org_id"], po.id)
    assert unchanged.status == PurchaseOrderStatus.APPROVED
    assert unchanged.items[0].quantity_received == Decimal("0")
    assert _inventory_on_hand(world) == Decimal("0")


def test_receive_goods_creates_inventory_transaction_linked_to_line_item(world):
    repo = _repo()
    po = _approved_po(world, quantity=Decimal("20"))
    item_id = po.items[0].id

    receipt = repo.receive_goods(
        world["org_id"], po.id,
        [GoodsReceiptLineInput(purchase_order_item_id=item_id, quantity=Decimal("20"))],
        world["user_id"])

    from app.models import InventoryTransaction
    with get_session() as session:
        tx = session.get(InventoryTransaction, receipt.items[0].inventory_transaction_id)
        assert tx is not None
        assert tx.transaction_type.value == "PURCHASE_RECEIVED"
        assert tx.quantity_change == Decimal("20")
        assert tx.reference_type == "purchase_order_item"
        assert tx.reference_id == item_id
        assert tx.performed_by == world["user_id"]


def test_receive_goods_records_audit_log_entry(world):
    repo = _repo()
    po = _approved_po(world, quantity=Decimal("15"))
    item_id = po.items[0].id

    repo.receive_goods(
        world["org_id"], po.id,
        [GoodsReceiptLineInput(purchase_order_item_id=item_id, quantity=Decimal("15"))],
        world["user_id"])

    with get_session() as session:
        entries = (session.query(AuditLog)
                  .filter_by(action="purchase_order.receive_goods", entity_id=po.id).all())
        assert len(entries) == 1
        assert entries[0].user_id == world["user_id"]
        assert entries[0].actor_email == "buyer@example.com"
        assert entries[0].changes["resulting_status"] == "RECEIVED"


def test_receive_goods_only_allowed_when_approved_or_partially_received(world):
    repo = _repo()
    po = _create_po(world, quantity=Decimal("5"))  # still DRAFT
    item_id = po.items[0].id

    with pytest.raises(Exception):  # InvalidPurchaseOrderTransitionError
        repo.receive_goods(world["org_id"], po.id,
                          [GoodsReceiptLineInput(purchase_order_item_id=item_id,
                                                 quantity=Decimal("1"))],
                          world["user_id"])


def test_receive_goods_multiple_line_items_in_one_receipt(world):
    repo = _repo()
    data = PurchaseOrderCreate(
        supplier_id=world["supplier_id"], warehouse_id=world["warehouse_id"],
        items=[PurchaseOrderItemInput(product_id=world["product_id"],
                                      quantity_ordered=Decimal("10"), unit_price=Decimal("5"),
                                      tax_percent=Decimal("0")),
              PurchaseOrderItemInput(product_id=world["product2_id"],
                                    quantity_ordered=Decimal("4"), unit_price=Decimal("8"),
                                    tax_percent=Decimal("0"))])
    po = repo.create(world["org_id"], data, world["user_id"])
    repo.submit(world["org_id"], po.id)
    po = repo.approve(world["org_id"], po.id, world["user_id"])

    lines = [GoodsReceiptLineInput(purchase_order_item_id=i.id, quantity=i.quantity_ordered)
            for i in po.items]
    repo.receive_goods(world["org_id"], po.id, lines, world["user_id"])

    final = repo.get_by_id(world["org_id"], po.id)
    assert final.status == PurchaseOrderStatus.RECEIVED
    assert all(i.quantity_received == i.quantity_ordered for i in final.items)

    inv_repo = SqlInventoryRepository()
    level1 = inv_repo.get_level(world["org_id"], world["product_id"], world["warehouse_id"])
    level2 = inv_repo.get_level(world["org_id"], world["product2_id"], world["warehouse_id"])
    assert level1.quantity_on_hand == Decimal("10")
    assert level2.quantity_on_hand == Decimal("4")


def test_receive_goods_rolls_back_earlier_lines_when_a_later_line_over_receives(world):
    """A single receive_goods call spanning multiple line items is one
    transaction (see the module docstring's explanation of why
    receive_goods reuses inventory_repository._apply directly rather than
    going through SqlInventoryRepository). If line 2 is rejected (over-
    receipt), line 1's already-applied inventory increase — earlier in the
    same Python loop, already flushed to the session — must not survive:
    proves the whole receipt is atomic, not "best effort per line".
    """
    repo = _repo()
    data = PurchaseOrderCreate(
        supplier_id=world["supplier_id"], warehouse_id=world["warehouse_id"],
        items=[PurchaseOrderItemInput(product_id=world["product_id"],
                                      quantity_ordered=Decimal("10"), unit_price=Decimal("5"),
                                      tax_percent=Decimal("0")),
              PurchaseOrderItemInput(product_id=world["product2_id"],
                                    quantity_ordered=Decimal("4"), unit_price=Decimal("8"),
                                    tax_percent=Decimal("0"))])
    po = repo.create(world["org_id"], data, world["user_id"])
    repo.submit(world["org_id"], po.id)
    po = repo.approve(world["org_id"], po.id, world["user_id"])

    lines = [
        GoodsReceiptLineInput(purchase_order_item_id=po.items[0].id,
                              quantity=Decimal("10")),  # legitimate, applied first
        GoodsReceiptLineInput(purchase_order_item_id=po.items[1].id,
                              quantity=Decimal("999")),  # over-receipt, rejected
    ]

    with pytest.raises(Exception):  # PurchaseOrderValidationError
        repo.receive_goods(world["org_id"], po.id, lines, world["user_id"])

    inv_repo = SqlInventoryRepository()
    level1 = inv_repo.get_level(world["org_id"], world["product_id"], world["warehouse_id"])
    level2 = inv_repo.get_level(world["org_id"], world["product2_id"], world["warehouse_id"])
    assert level1.quantity_on_hand == Decimal("0")   # line 1's increase did NOT persist
    assert level2.quantity_on_hand == Decimal("0")

    unchanged = repo.get_by_id(world["org_id"], po.id)
    assert unchanged.status == PurchaseOrderStatus.APPROVED  # never advanced to RECEIVED
    assert all(i.quantity_received == Decimal("0") for i in unchanged.items)

    with get_session() as session:
        assert session.query(GoodsReceipt).filter_by(purchase_order_id=po.id).count() == 0


# -- purchase returns -------------------------------------------------------#

def test_record_return_reduces_inventory_and_net_received(world):
    repo = _repo()
    po = _approved_po(world, quantity=Decimal("20"))
    item_id = po.items[0].id
    repo.receive_goods(world["org_id"], po.id,
                       [GoodsReceiptLineInput(purchase_order_item_id=item_id,
                                              quantity=Decimal("20"))],
                       world["user_id"])

    result = repo.record_return(world["org_id"], po.id, item_id, Decimal("3"),
                                "damaged in transit", world["user_id"])

    assert result.quantity == Decimal("3")
    assert _inventory_on_hand(world) == Decimal("17")

    updated_po = repo.get_by_id(world["org_id"], po.id)
    assert updated_po.items[0].quantity_received == Decimal("17")


def test_record_return_rejects_more_than_received(world):
    repo = _repo()
    po = _approved_po(world, quantity=Decimal("10"))
    item_id = po.items[0].id
    repo.receive_goods(world["org_id"], po.id,
                       [GoodsReceiptLineInput(purchase_order_item_id=item_id,
                                              quantity=Decimal("4"))],
                       world["user_id"])

    with pytest.raises(Exception):  # PurchaseOrderValidationError
        repo.record_return(world["org_id"], po.id, item_id, Decimal("5"), "too many",
                          world["user_id"])

    # Nothing changed from the failed attempt.
    assert _inventory_on_hand(world) == Decimal("4")


def test_record_return_creates_return_out_ledger_entry(world):
    repo = _repo()
    po = _approved_po(world, quantity=Decimal("10"))
    item_id = po.items[0].id
    repo.receive_goods(world["org_id"], po.id,
                       [GoodsReceiptLineInput(purchase_order_item_id=item_id,
                                              quantity=Decimal("10"))],
                       world["user_id"])

    result = repo.record_return(world["org_id"], po.id, item_id, Decimal("2"), "wrong item",
                                world["user_id"])

    from app.models import InventoryTransaction
    with get_session() as session:
        tx = session.get(InventoryTransaction, result.inventory_transaction_id)
        assert tx.transaction_type.value == "RETURN_OUT"
        assert tx.quantity_change == Decimal("-2")


# -- supplier repository -----------------------------------------------------#

def test_supplier_repository_scopes_by_organization(world):
    with get_session() as session:
        other_org = Organization(name="Other Org")
        session.add(other_org)
        session.flush()
        other_org_id = other_org.id

    repo = SqlSupplierRepository()
    created = repo.create(world["org_id"], SupplierCreate(name="New Supplier"))
    assert repo.get_by_id(other_org_id, created.id) is None
    assert repo.get_by_id(world["org_id"], created.id) is not None
