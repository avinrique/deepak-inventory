"""
Storage layer for the Inventory Management app.

All Excel reading/writing and business rules live here so they can be tested
without a GUI. The UI (``inventory_app.py``) only calls these functions.

A single bill may contain many products. Each product becomes one row in the
sales/purchases workbook, all sharing the same Bill No. Per-line figures
(Quantity, Rate, Amount = Quantity x Rate) differ per row; bill-level figures
(Subtotal, ECS, VAT %, VAT Amount, Total) are repeated on every row of the
bill so each row is fully self-contained and nothing reads as blank.

Files live in a stable per-user folder (see ``data_dir``), deliberately OUTSIDE
the project/exe directory so they survive rebuilds, re-downloads and git
clean/checkpoint restores. Override with ``INVENTORY_DATA_DIR``.
    sales.xlsx       - every sale (one row per product line)
    purchases.xlsx   - every purchase (one row per product line)
    stock.xlsx       - current quantity on hand, per product
    party.xlsx       - per-party totals (sales / purchases / combined)
"""

import os
import sys
import math
import shutil
import platform
import subprocess

from openpyxl import Workbook, load_workbook


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class FileLockedError(Exception):
    """A workbook could not be written (usually open in Excel/LibreOffice)."""

    def __init__(self, path):
        self.path = path
        super().__init__(path)


# --------------------------------------------------------------------------- #
# Storage location
# --------------------------------------------------------------------------- #
def _user_data_base() -> str:
    """Per-user, OS-appropriate folder for application data.

    Deliberately OUTSIDE the project/exe folder so the data can never be wiped
    by a git operation (the project ``.gitignore`` excludes ``inventory_data``),
    a rebuild that recreates ``dist``, a re-download, or a checkpoint/clean
    that restores the working tree to a snapshot. Override with the
    ``INVENTORY_DATA_DIR`` environment variable.
    """
    override = os.environ.get("INVENTORY_DATA_DIR")
    if override:
        return override

    system = platform.system()
    home = os.path.expanduser("~")
    if system == "Windows":
        root = os.environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
    elif system == "Darwin":
        root = os.path.join(home, "Library", "Application Support")
    else:
        root = os.environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
    return os.path.join(root, "InventoryManagement")


def _legacy_data_dir() -> str:
    """The old location: ``inventory_data`` next to the exe (frozen) or module."""
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "inventory_data")


def data_dir() -> str:
    """Folder where the Excel files live (stable, per-user, outside the repo).

    One-time migration: if the new folder has no workbooks yet but the old
    ``inventory_data`` folder next to the app does, copy them across so an
    existing user keeps their data.
    """
    folder = os.path.join(_user_data_base(), "inventory_data")
    os.makedirs(folder, exist_ok=True)

    legacy = _legacy_data_dir()
    if os.path.abspath(legacy) != os.path.abspath(folder) and os.path.isdir(legacy):
        for name in ("sales.xlsx", "purchases.xlsx", "stock.xlsx", "party.xlsx"):
            src, dst = os.path.join(legacy, name), os.path.join(folder, name)
            if os.path.exists(src) and not os.path.exists(dst):
                shutil.copy2(src, dst)
    return folder


DATA_DIR = data_dir()
SALES_FILE = os.path.join(DATA_DIR, "sales.xlsx")
PURCHASES_FILE = os.path.join(DATA_DIR, "purchases.xlsx")
STOCK_FILE = os.path.join(DATA_DIR, "stock.xlsx")
PARTY_FILE = os.path.join(DATA_DIR, "party.xlsx")

TXN_HEADERS = [
    "Date", "Bill No", "PAN No", "Vendor Name", "Vendor Address",
    "Product Name", "Quantity", "Rate", "Amount",
    "Subtotal", "ECS", "VAT %", "VAT Amount", "Total",
]
STOCK_HEADERS = ["Product Name", "Quantity"]
PARTY_HEADERS = [
    "PAN No", "Vendor Name", "Vendor Address",
    "Total Sales", "Total Purchases", "Total Combined",
]


# --------------------------------------------------------------------------- #
# Low-level helpers
# --------------------------------------------------------------------------- #
def _save(wb, path: str) -> None:
    """Save a workbook, turning a lock/IO error into FileLockedError."""
    try:
        wb.save(path)
    except (PermissionError, OSError) as exc:
        raise FileLockedError(path) from exc


def _ensure_file(path: str, headers: list) -> None:
    """Create the workbook with a header row if it does not exist yet."""
    if os.path.exists(path):
        return
    wb = Workbook()
    ws = wb.active
    ws.append(headers)
    _save(wb, path)


def _load(path: str, headers: list):
    """Return (workbook, worksheet), creating the file if missing."""
    _ensure_file(path, headers)
    wb = load_workbook(path)
    return wb, wb.active


def assert_writable(*paths) -> None:
    """Raise FileLockedError if any existing file is locked for writing.

    Lets a commit fail BEFORE it writes anything, so a workbook left open in
    Excel/LibreOffice can never cause a half-applied bill.
    """
    for p in paths:
        if os.path.exists(p):
            try:
                with open(p, "r+b"):
                    pass
            except (PermissionError, OSError) as exc:
                raise FileLockedError(p) from exc


def _safe_text(value) -> str:
    """Neutralize spreadsheet formula injection in a free-text cell.

    A value beginning with = + - @ is prefixed with ' so Excel/LibreOffice
    stores it as literal text rather than evaluating it as a formula.
    """
    s = "" if value is None else str(value)
    return "'" + s if s[:1] in ("=", "+", "-", "@") else s


def num(value) -> float:
    """Parse a value into a float; blanks/junk/non-finite become 0.

    A trailing percent sign and thousands commas are tolerated, so "13%" and
    "1,030" parse as 13 and 1030.
    """
    if value is None:
        return 0.0
    try:
        result = float(str(value).replace(",", "").replace("%", "").strip() or 0)
    except ValueError:
        return 0.0
    return result if math.isfinite(result) else 0.0


# --------------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------------- #
def append_bill(path: str, header: dict, lines: list, subtotal: float,
                ecs: float, vat_pct: float, vat_amount: float,
                total: float) -> None:
    """Write a multi-product bill: one row per product line.

    ``header`` has date/bill/pan/vendor/address. ``lines`` is a list of dicts
    with product/qty/rate/amount. Bill-level figures (Subtotal, ECS, VAT %,
    VAT Amount, Total) are repeated on every row so each row is self-contained.
    """
    wb, ws = _load(path, TXN_HEADERS)
    for ln in lines:
        ws.append([
            _safe_text(header["date"]), _safe_text(header["bill"]),
            _safe_text(header["pan"]), _safe_text(header["vendor"]),
            _safe_text(header["address"]),
            _safe_text(ln["product"]), ln["qty"], ln["rate"], ln["amount"],
            subtotal, ecs, vat_pct, vat_amount, total,
        ])
    _save(wb, path)


def update_stock(product: str, qty: float, add: bool) -> float:
    """Add to (purchase) or subtract from (sale) stock for a product.

    Returns the resulting quantity on hand. Matching is case-insensitive on
    the trimmed product name; a new row is created when the product is unseen.
    """
    wb, ws = _load(STOCK_FILE, STOCK_HEADERS)
    key = product.strip().lower()
    for r in range(2, ws.max_row + 1):
        name = ws.cell(row=r, column=1).value
        if name is not None and str(name).strip().lower() == key:
            current = num(ws.cell(row=r, column=2).value)
            new_qty = current + qty if add else current - qty
            ws.cell(row=r, column=2, value=new_qty)
            _save(wb, STOCK_FILE)
            return new_qty
    new_qty = qty if add else -qty
    ws.append([_safe_text(product.strip()), new_qty])
    _save(wb, STOCK_FILE)
    return new_qty


def stock_on_hand(product: str) -> float:
    """Current quantity for a product (0 if unknown)."""
    _ensure_file(STOCK_FILE, STOCK_HEADERS)
    wb = load_workbook(STOCK_FILE)
    ws = wb.active
    key = product.strip().lower()
    for r in range(2, ws.max_row + 1):
        name = ws.cell(row=r, column=1).value
        if name is not None and str(name).strip().lower() == key:
            return num(ws.cell(row=r, column=2).value)
    return 0.0


def update_party(pan: str, name: str, address: str, total: float,
                 is_sale: bool) -> None:
    """Update a party's running totals.

    Keyed by PAN when present; otherwise by Vendor Name + Address together
    (so two different blank-PAN vendors are not merged). A bill with neither
    PAN nor name is bucketed under a single "Unknown" party.
    """
    key_pan = pan.strip().lower()
    key_name = name.strip().lower()
    key_addr = address.strip().lower()
    if not key_pan and not key_name:
        name, key_name = "Unknown", "unknown"

    wb, ws = _load(PARTY_FILE, PARTY_HEADERS)

    def matches(row_pan, row_name, row_addr) -> bool:
        rp = str(row_pan).strip().lower() if row_pan is not None else ""
        rn = str(row_name).strip().lower() if row_name is not None else ""
        ra = str(row_addr).strip().lower() if row_addr is not None else ""
        if key_pan:
            return rp == key_pan
        return rn == key_name and ra == key_addr

    for r in range(2, ws.max_row + 1):
        if matches(ws.cell(row=r, column=1).value,
                   ws.cell(row=r, column=2).value,
                   ws.cell(row=r, column=3).value):
            sales = num(ws.cell(row=r, column=4).value)
            purch = num(ws.cell(row=r, column=5).value)
            if is_sale:
                sales += total
            else:
                purch += total
            ws.cell(row=r, column=4, value=sales)
            ws.cell(row=r, column=5, value=purch)
            ws.cell(row=r, column=6, value=sales + purch)
            if name.strip():
                ws.cell(row=r, column=2, value=_safe_text(name.strip()))
            if address.strip():
                ws.cell(row=r, column=3, value=_safe_text(address.strip()))
            _save(wb, PARTY_FILE)
            return

    sales = total if is_sale else 0.0
    purch = 0.0 if is_sale else total
    ws.append([_safe_text(pan.strip()), _safe_text(name.strip()),
               _safe_text(address.strip()), sales, purch, sales + purch])
    _save(wb, PARTY_FILE)


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #
def read_rows(path: str, headers: list) -> list:
    """Return all data rows (excluding the header) as lists."""
    _ensure_file(path, headers)
    wb = load_workbook(path)
    ws = wb.active
    rows = []
    for r in range(2, ws.max_row + 1):
        row = [ws.cell(row=r, column=c).value for c in range(1, len(headers) + 1)]
        if any(v is not None and str(v).strip() != "" for v in row):
            rows.append(row)
    return rows


def product_names() -> list:
    """Distinct product names currently known in stock (for the dropdown)."""
    _ensure_file(STOCK_FILE, STOCK_HEADERS)
    wb = load_workbook(STOCK_FILE)
    ws = wb.active
    names, seen = [], set()
    for r in range(2, ws.max_row + 1):
        name = ws.cell(row=r, column=1).value
        if name is not None:
            text = str(name).strip()
            key = text.lower()
            if text and key not in seen:
                seen.add(key)
                names.append(text)
    return sorted(names, key=str.lower)


def bill_exists(path: str, bill_no: str) -> bool:
    """True if a non-blank Bill No already appears in this ledger."""
    bill_no = (bill_no or "").strip()
    if not bill_no:
        return False
    _ensure_file(path, TXN_HEADERS)
    ws = load_workbook(path).active
    for r in range(2, ws.max_row + 1):
        v = ws.cell(row=r, column=2).value  # "Bill No" column
        if v is not None and str(v).strip().lower() == bill_no.lower():
            return True
    return False


def open_file(path: str, headers: list):
    """Open a workbook in the OS default app. Returns (ok, error_message)."""
    try:
        _ensure_file(path, headers)
        system = platform.system()
        if system == "Windows":
            os.startfile(path)  # type: ignore[attr-defined]
        elif system == "Darwin":
            res = subprocess.run(["open", path])
            if res.returncode != 0:
                return False, f"'open' exited with code {res.returncode}."
        else:
            res = subprocess.run(["xdg-open", path])
            if res.returncode != 0:
                return False, f"'xdg-open' exited with code {res.returncode}."
        return True, ""
    except Exception as exc:  # noqa: BLE001 - surface any launch/IO failure
        return False, str(exc)
