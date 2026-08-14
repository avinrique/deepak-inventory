"""Pure inventory validation/enums — Decimal-based, no I/O, no framework.

InventoryTransactionType lives here (not app.models or app.schemas) for the
same reason as app.domain.product.ProductStatus: the ORM model and the
Pydantic schema share one source of truth instead of two enums that could
drift.

Sign convention: every InventoryTransaction.quantity_change is signed. The
*_IN / *_RECEIVED / RESERVE types are positive; the *_OUT / DAMAGE /
RELEASE_RESERVE types are negative. ADJUSTMENT carries whatever signed
value the caller supplies (a correction can go either direction). Service
methods take an unsigned ``quantity`` from the caller and apply the correct
sign for the operation — see app.services.inventory_service.
"""
from decimal import Decimal
from enum import Enum


class InventoryTransactionType(str, Enum):
    STOCK_IN = "STOCK_IN"                # generic receipt not tied to a purchase
    PURCHASE_RECEIVED = "PURCHASE_RECEIVED"
    STOCK_OUT = "STOCK_OUT"              # generic manual issue not tied to a sale
    SALE = "SALE"
    DAMAGE = "DAMAGE"
    RETURN_IN = "RETURN_IN"              # customer return back into stock
    RETURN_OUT = "RETURN_OUT"            # return to supplier, leaves stock
    ADJUSTMENT = "ADJUSTMENT"            # signed manual correction (recount, etc.)
    TRANSFER_OUT = "TRANSFER_OUT"
    TRANSFER_IN = "TRANSFER_IN"
    RESERVE = "RESERVE"                  # earmarks stock without removing it
    RELEASE_RESERVE = "RELEASE_RESERVE"


# Types that move quantity_on_hand. Every other type (RESERVE /
# RELEASE_RESERVE) moves quantity_reserved instead — see
# InventoryRepository.apply_transaction, the one place this distinction is
# acted on.
ON_HAND_TYPES: frozenset[InventoryTransactionType] = frozenset({
    InventoryTransactionType.STOCK_IN,
    InventoryTransactionType.PURCHASE_RECEIVED,
    InventoryTransactionType.STOCK_OUT,
    InventoryTransactionType.SALE,
    InventoryTransactionType.DAMAGE,
    InventoryTransactionType.RETURN_IN,
    InventoryTransactionType.RETURN_OUT,
    InventoryTransactionType.ADJUSTMENT,
    InventoryTransactionType.TRANSFER_OUT,
    InventoryTransactionType.TRANSFER_IN,
})
RESERVED_TYPES: frozenset[InventoryTransactionType] = frozenset({
    InventoryTransactionType.RESERVE,
    InventoryTransactionType.RELEASE_RESERVE,
})

# Direction each type applies to an unsigned caller-supplied quantity.
# ADJUSTMENT is intentionally absent: its sign comes directly from the
# caller (a correction can be positive or negative), not from the type.
_SIGN: dict[InventoryTransactionType, int] = {
    InventoryTransactionType.STOCK_IN: 1,
    InventoryTransactionType.PURCHASE_RECEIVED: 1,
    InventoryTransactionType.STOCK_OUT: -1,
    InventoryTransactionType.SALE: -1,
    InventoryTransactionType.DAMAGE: -1,
    InventoryTransactionType.RETURN_IN: 1,
    InventoryTransactionType.RETURN_OUT: -1,
    InventoryTransactionType.TRANSFER_OUT: -1,
    InventoryTransactionType.TRANSFER_IN: 1,
    InventoryTransactionType.RESERVE: 1,
    InventoryTransactionType.RELEASE_RESERVE: -1,
}


def signed_quantity_change(transaction_type: InventoryTransactionType,
                           quantity: Decimal) -> Decimal:
    """Turns a caller-supplied unsigned ``quantity`` (always > 0) into the
    signed delta stored on the ledger row, for every type except
    ADJUSTMENT, which is already signed on the way in.
    """
    if transaction_type == InventoryTransactionType.ADJUSTMENT:
        return quantity
    return _SIGN[transaction_type] * quantity


def validate_quantity(quantity: Decimal, *, allow_negative: bool = False) -> list[str]:
    """Shared shape check for every operation's input quantity. Does NOT
    check quantity against the current stock level — that requires the
    locked Inventory row and lives in the repository, where the lock is
    held (see InventoryRepository.apply_transaction).
    """
    errors = []
    if quantity == 0:
        errors.append("Quantity must not be zero.")
    if not allow_negative and quantity < 0:
        errors.append("Quantity must be positive.")
    return errors


def validate_warehouse(*, code: str, name: str) -> list[str]:
    errors = []
    if not code.strip():
        errors.append("Warehouse code is required.")
    if not name.strip():
        errors.append("Warehouse name is required.")
    return errors


def normalize_warehouse_code(code: str) -> str:
    return code.strip().upper()


class LowStockBehavior(str, Enum):
    """Organization-level policy for what happens when a sale would take a
    product's on-hand quantity below its Product.minimum_stock_level —
    see InventoryRepository._apply's ``enforce_low_stock_policy`` param,
    used only by SalesOrderRepository.fulfill_sale. Deliberately not
    enforced on manual adjustments/damage/transfers: those are the tools
    an operator uses to *fix* a low-stock situation, so blocking them on
    the same policy would be self-defeating.
    """
    WARN_ONLY = "WARN_ONLY"      # no blocking — reporting/dashboard still flags it
    BLOCK_SALE = "BLOCK_SALE"    # fulfill_sale refuses a sale that would breach it


class StockValuationMethod(str, Enum):
    """Which costing method inventory valuation is reported under.

    Honesty note: this codebase has no per-receipt cost-layer history
    (InventoryTransaction does not carry a unit cost) — valuation is
    currently always computed from Product.purchase_price, the same
    "standard cost" figure regardless of which method is selected here.
    Storing and surfacing the choice now is still worthwhile: it is real,
    validated, persisted configuration ready for a future costing engine
    to consume, and the report labels which method is configured rather
    than silently ignoring the setting. See
    SqlReportingRepository.inventory_valuation_report.
    """
    FIFO = "FIFO"
    LIFO = "LIFO"
    WEIGHTED_AVERAGE = "WEIGHTED_AVERAGE"
