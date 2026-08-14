"""SqlInventoryRepository (and SqlWarehouseRepository) against a live
PostgreSQL database — proves the row-locking, negative-stock guard, and
audit-trail behavior actually hold under real SQL and real concurrent
threads, not just the fake-repository/single-threaded assumptions in
tests/services/test_inventory_service.py.

Uses the ``live_db`` fixture (tests/conftest.py) — see its docstring for
why this is gated on INVENTORY_TEST_DATABASE_URL, separate from the app's
real INVENTORY_DATABASE_URL, and how to run these locally.
"""
import threading
import uuid
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from app.database.session import get_session
from app.domain.inventory import InventoryTransactionType
from app.models import Inventory, InventoryTransaction, Organization, Product, Unit, User, Warehouse
from app.repositories.sql.inventory_repository import SqlInventoryRepository
from app.repositories.sql.warehouse_repository import SqlWarehouseRepository
from app.schemas.inventory import TransactionFilter, WarehouseCreate


@pytest.fixture()
def world(live_db):
    """One organization, one product, two warehouses, one user — the
    minimum graph every inventory operation needs to satisfy its FKs.
    """
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
        warehouse_a = Warehouse(organization_id=org.id, code="MAIN", name="Main")
        warehouse_b = Warehouse(organization_id=org.id, code="SECOND", name="Second")
        user = User(email="tester@example.com", hashed_password="x", full_name="Tester")
        session.add_all([product, warehouse_a, warehouse_b, user])
        session.flush()
        return {
            "org_id": org.id, "product_id": product.id,
            "warehouse_a_id": warehouse_a.id, "warehouse_b_id": warehouse_b.id,
            "user_id": user.id,
        }


def _repo():
    return SqlInventoryRepository()


# -- stock in / out / damage / return ------------------------------------#

def test_stock_in_creates_inventory_row_and_ledger_entry(world):
    repo = _repo()
    tx = repo.stock_in(world["org_id"], world["product_id"], world["warehouse_a_id"],
                       Decimal("100"), world["user_id"], notes="opening stock")

    assert tx.transaction_type == InventoryTransactionType.STOCK_IN
    assert tx.quantity_change == Decimal("100")
    assert tx.quantity_on_hand_after == Decimal("100")

    level = repo.get_level(world["org_id"], world["product_id"], world["warehouse_a_id"])
    assert level.quantity_on_hand == Decimal("100")


def test_stock_out_reduces_on_hand_and_records_ledger(world):
    repo = _repo()
    repo.stock_in(world["org_id"], world["product_id"], world["warehouse_a_id"],
                  Decimal("50"), world["user_id"])
    tx = repo.stock_out(world["org_id"], world["product_id"], world["warehouse_a_id"],
                        Decimal("20"), world["user_id"])

    assert tx.quantity_change == Decimal("-20")
    assert tx.quantity_on_hand_after == Decimal("30")


def test_stock_out_beyond_on_hand_rejected_by_default(world):
    repo = _repo()
    repo.stock_in(world["org_id"], world["product_id"], world["warehouse_a_id"],
                  Decimal("10"), world["user_id"])
    with pytest.raises(Exception):  # InsufficientStockError
        repo.stock_out(world["org_id"], world["product_id"], world["warehouse_a_id"],
                       Decimal("20"), world["user_id"])
    # Nothing committed from the failed attempt.
    level = repo.get_level(world["org_id"], world["product_id"], world["warehouse_a_id"])
    assert level.quantity_on_hand == Decimal("10")


def test_negative_stock_allowed_when_organization_opts_in(world):
    with get_session() as session:
        org = session.get(Organization, world["org_id"])
        org.allow_negative_stock = True

    repo = _repo()
    tx = repo.stock_out(world["org_id"], world["product_id"], world["warehouse_a_id"],
                        Decimal("5"), world["user_id"])
    assert tx.quantity_on_hand_after == Decimal("-5")


def test_mark_damaged_reduces_on_hand(world):
    repo = _repo()
    repo.stock_in(world["org_id"], world["product_id"], world["warehouse_a_id"],
                  Decimal("10"), world["user_id"])
    tx = repo.mark_damaged(world["org_id"], world["product_id"], world["warehouse_a_id"],
                           Decimal("3"), world["user_id"], notes="dropped crate")
    assert tx.transaction_type == InventoryTransactionType.DAMAGE
    assert tx.quantity_on_hand_after == Decimal("7")


def test_record_return_to_stock_and_to_supplier(world):
    repo = _repo()
    in_tx = repo.record_return(world["org_id"], world["product_id"], world["warehouse_a_id"],
                               Decimal("4"), world["user_id"], to_stock=True)
    assert in_tx.transaction_type == InventoryTransactionType.RETURN_IN
    assert in_tx.quantity_on_hand_after == Decimal("4")

    out_tx = repo.record_return(world["org_id"], world["product_id"], world["warehouse_a_id"],
                                Decimal("1"), world["user_id"], to_stock=False)
    assert out_tx.transaction_type == InventoryTransactionType.RETURN_OUT
    assert out_tx.quantity_on_hand_after == Decimal("3")


# -- adjustment ------------------------------------------------------------#

def test_adjustment_creates_stock_adjustment_row_linked_to_ledger_entry(world):
    repo = _repo()
    repo.stock_in(world["org_id"], world["product_id"], world["warehouse_a_id"],
                  Decimal("10"), world["user_id"])
    tx = repo.adjust(world["org_id"], world["product_id"], world["warehouse_a_id"],
                     Decimal("-2"), "Recount shortfall", world["user_id"])

    assert tx.transaction_type == InventoryTransactionType.ADJUSTMENT
    assert tx.quantity_on_hand_after == Decimal("8")

    with get_session() as session:
        from app.models import StockAdjustment
        adjustment = (session.query(StockAdjustment)
                     .filter_by(inventory_transaction_id=tx.id).first())
        assert adjustment is not None
        assert adjustment.reason == "Recount shortfall"
        assert adjustment.quantity_change == Decimal("-2")


# -- transfer ----------------------------------------------------------- #

def test_transfer_moves_stock_and_records_stock_transfer_row(world):
    repo = _repo()
    repo.stock_in(world["org_id"], world["product_id"], world["warehouse_a_id"],
                  Decimal("10"), world["user_id"])

    out_tx, in_tx = repo.transfer(world["org_id"], world["product_id"],
                                  world["warehouse_a_id"], world["warehouse_b_id"],
                                  Decimal("6"), world["user_id"], notes="rebalance")

    assert out_tx.transaction_type == InventoryTransactionType.TRANSFER_OUT
    assert out_tx.quantity_on_hand_after == Decimal("4")
    assert in_tx.transaction_type == InventoryTransactionType.TRANSFER_IN
    assert in_tx.quantity_on_hand_after == Decimal("6")

    with get_session() as session:
        from app.models import StockTransfer
        transfer = (session.query(StockTransfer)
                   .filter_by(out_transaction_id=out_tx.id).first())
        assert transfer is not None
        assert transfer.in_transaction_id == in_tx.id
        assert transfer.quantity == Decimal("6")


def test_transfer_insufficient_source_stock_rolls_back_both_sides(world):
    repo = _repo()
    repo.stock_in(world["org_id"], world["product_id"], world["warehouse_a_id"],
                  Decimal("2"), world["user_id"])

    with pytest.raises(Exception):
        repo.transfer(world["org_id"], world["product_id"], world["warehouse_a_id"],
                     world["warehouse_b_id"], Decimal("10"), world["user_id"])

    # Destination must not have gained anything from the half-applied
    # attempt — the whole transfer is one database transaction.
    dest_level = repo.get_level(world["org_id"], world["product_id"], world["warehouse_b_id"])
    assert dest_level.quantity_on_hand == Decimal("0")
    src_level = repo.get_level(world["org_id"], world["product_id"], world["warehouse_a_id"])
    assert src_level.quantity_on_hand == Decimal("2")


def test_transfer_rejects_same_warehouse(world):
    repo = _repo()
    with pytest.raises(Exception):  # InvalidTransferError
        repo.transfer(world["org_id"], world["product_id"], world["warehouse_a_id"],
                     world["warehouse_a_id"], Decimal("1"), world["user_id"])


# -- reserve / release ---------------------------------------------------#

def test_reserve_then_release_round_trips(world):
    repo = _repo()
    repo.stock_in(world["org_id"], world["product_id"], world["warehouse_a_id"],
                  Decimal("10"), world["user_id"])
    reserve_tx = repo.reserve(world["org_id"], world["product_id"], world["warehouse_a_id"],
                              Decimal("4"), world["user_id"])
    assert reserve_tx.quantity_reserved_after == Decimal("4")

    release_tx = repo.release_reservation(world["org_id"], world["product_id"],
                                          world["warehouse_a_id"], Decimal("4"),
                                          world["user_id"])
    assert release_tx.quantity_reserved_after == Decimal("0")


def test_reserve_more_than_on_hand_rejected(world):
    repo = _repo()
    repo.stock_in(world["org_id"], world["product_id"], world["warehouse_a_id"],
                  Decimal("5"), world["user_id"])
    with pytest.raises(Exception):
        repo.reserve(world["org_id"], world["product_id"], world["warehouse_a_id"],
                    Decimal("6"), world["user_id"])


def test_on_hand_cannot_drop_below_reserved_even_with_negative_stock_allowed(world):
    with get_session() as session:
        org = session.get(Organization, world["org_id"])
        org.allow_negative_stock = True

    repo = _repo()
    repo.stock_in(world["org_id"], world["product_id"], world["warehouse_a_id"],
                 Decimal("10"), world["user_id"])
    repo.reserve(world["org_id"], world["product_id"], world["warehouse_a_id"],
                Decimal("8"), world["user_id"])

    with pytest.raises(Exception):  # would drop on-hand (10-5=5) below reserved (8)
        repo.stock_out(world["org_id"], world["product_id"], world["warehouse_a_id"],
                       Decimal("5"), world["user_id"])


# -- audit trail: user + timestamp + immutability ------------------------#

def test_ledger_entry_records_performing_user_and_timestamp(world):
    repo = _repo()
    tx = repo.stock_in(world["org_id"], world["product_id"], world["warehouse_a_id"],
                       Decimal("1"), world["user_id"])
    assert tx.performed_by == world["user_id"]
    assert tx.created_at is not None


def test_transaction_history_accumulates_every_operation(world):
    repo = _repo()
    repo.stock_in(world["org_id"], world["product_id"], world["warehouse_a_id"],
                 Decimal("100"), world["user_id"])
    repo.stock_out(world["org_id"], world["product_id"], world["warehouse_a_id"],
                  Decimal("20"), world["user_id"])
    repo.mark_damaged(world["org_id"], world["product_id"], world["warehouse_a_id"],
                      Decimal("5"), world["user_id"])
    repo.adjust(world["org_id"], world["product_id"], world["warehouse_a_id"],
               Decimal("2"), "correction", world["user_id"])

    page = repo.list_transactions(world["org_id"], TransactionFilter(page_size=100))
    types = [t.transaction_type for t in page.items]
    assert page.total == 4
    assert InventoryTransactionType.STOCK_IN in types
    assert InventoryTransactionType.STOCK_OUT in types
    assert InventoryTransactionType.DAMAGE in types
    assert InventoryTransactionType.ADJUSTMENT in types
    # PURCHASE_RECEIVED +100 / SALE -20 / DAMAGE -5 / ADJUSTMENT +2 style
    # ledger — the running on_hand_after must reflect the running sum.
    by_created = sorted(page.items, key=lambda t: t.created_at)
    assert by_created[-1].quantity_on_hand_after == Decimal("100") - Decimal("20") \
        - Decimal("5") + Decimal("2")


# -- concurrency ---------------------------------------------------------#

def test_concurrent_stock_out_never_oversells(world):
    """20 threads each try to sell 1 unit against 10 units on hand, all at
    once. Row-level locking (SELECT ... FOR UPDATE in
    InventoryRepository.apply_transaction) must serialize them: exactly 10
    succeed, exactly 10 fail with InsufficientStockError, and on-hand never
    goes negative or drifts from a lost update.
    """
    repo = _repo()
    repo.stock_in(world["org_id"], world["product_id"], world["warehouse_a_id"],
                 Decimal("10"), world["user_id"])

    results = []
    lock = threading.Lock()

    def sell_one():
        try:
            repo.stock_out(world["org_id"], world["product_id"], world["warehouse_a_id"],
                          Decimal("1"), world["user_id"])
            outcome = "ok"
        except Exception:
            outcome = "rejected"
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=sell_one) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count("ok") == 10
    assert results.count("rejected") == 10

    level = repo.get_level(world["org_id"], world["product_id"], world["warehouse_a_id"])
    assert level.quantity_on_hand == Decimal("0")

    # Every successful sale left exactly one ledger row — no lost updates,
    # no double-counted rows.
    page = repo.list_transactions(world["org_id"], TransactionFilter(
        transaction_type=InventoryTransactionType.STOCK_OUT, page_size=100))
    assert page.total == 10


def test_concurrent_first_ever_write_to_a_product_warehouse_pair_is_safe(world):
    """No Inventory row exists yet for this (product, warehouse) pair —
    several threads racing to create it for the first time must not
    corrupt the count or crash outside the expected exception path (the
    unique-index/SAVEPOINT race in _lock_or_create_inventory_row).
    """
    repo = _repo()
    results = []
    lock = threading.Lock()

    def add_one():
        try:
            repo.stock_in(world["org_id"], world["product_id"], world["warehouse_a_id"],
                         Decimal("1"), world["user_id"])
            with lock:
                results.append("ok")
        except Exception as exc:
            with lock:
                results.append(f"error:{exc}")

    threads = [threading.Thread(target=add_one) for _ in range(15)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results == ["ok"] * 15, results
    level = repo.get_level(world["org_id"], world["product_id"], world["warehouse_a_id"])
    assert level.quantity_on_hand == Decimal("15")


def test_concurrent_transfers_in_opposite_directions_do_not_deadlock(world):
    """A -> B and B -> A transfers running concurrently lock the same two
    Inventory rows in opposite "natural" order; the canonical lock order in
    SqlInventoryRepository.transfer must prevent a deadlock. If it didn't,
    Postgres would eventually abort one side with a deadlock error — this
    test's join() would hang forever without the fix, so the assertions
    below (reached at all) are themselves part of what's being proven.
    """
    repo = _repo()
    repo.stock_in(world["org_id"], world["product_id"], world["warehouse_a_id"],
                 Decimal("100"), world["user_id"])
    repo.stock_in(world["org_id"], world["product_id"], world["warehouse_b_id"],
                 Decimal("100"), world["user_id"])

    errors = []

    def transfer_a_to_b():
        for _ in range(10):
            try:
                repo.transfer(world["org_id"], world["product_id"], world["warehouse_a_id"],
                             world["warehouse_b_id"], Decimal("1"), world["user_id"])
            except Exception as exc:  # noqa: BLE001 - recorded, not raised, in a thread
                errors.append(exc)

    def transfer_b_to_a():
        for _ in range(10):
            try:
                repo.transfer(world["org_id"], world["product_id"], world["warehouse_b_id"],
                             world["warehouse_a_id"], Decimal("1"), world["user_id"])
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

    t1 = threading.Thread(target=transfer_a_to_b)
    t2 = threading.Thread(target=transfer_b_to_a)
    t1.start()
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)

    assert not t1.is_alive() and not t2.is_alive(), "threads still running — deadlock"
    assert errors == [], [str(e) for e in errors]

    level_a = repo.get_level(world["org_id"], world["product_id"], world["warehouse_a_id"])
    level_b = repo.get_level(world["org_id"], world["product_id"], world["warehouse_b_id"])
    assert level_a.quantity_on_hand + level_b.quantity_on_hand == Decimal("200")


# -- warehouse repository -------------------------------------------------#

def test_warehouse_repository_scopes_by_organization(world):
    with get_session() as session:
        other_org = Organization(name="Other Org")
        session.add(other_org)
        session.flush()
        other_org_id = other_org.id

    repo = SqlWarehouseRepository()
    created = repo.create(world["org_id"], WarehouseCreate(code="EXTRA", name="Extra"))
    assert repo.get_by_id(other_org_id, created.id) is None
    assert repo.get_by_id(world["org_id"], created.id) is not None


def test_warehouse_code_unique_per_organization(world):
    repo = SqlWarehouseRepository()
    repo.create(world["org_id"], WarehouseCreate(code="DUPLICATE", name="First"))
    with pytest.raises(IntegrityError):
        repo.create(world["org_id"], WarehouseCreate(code="DUPLICATE", name="Second"))
