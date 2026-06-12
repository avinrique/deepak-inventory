"""
Storage layer for the Inventory Management app.

All Excel reading/writing and business rules live here so they can be tested
without a GUI. The UI (``inventory_app.py``) only calls these functions.

A single bill may contain many products. Each product becomes one row in the
sales/purchases workbook, all sharing the same Bill No. Per-line figures
(Quantity, Rate, Amount = Quantity x Rate) differ per row; bill-level figures
(ECS, VAT %, VAT Amount, Total) are repeated on every row of the bill so each
row is fully self-contained and nothing reads as blank.

Files created next to the app (or next to the .exe when frozen):
    sales.xlsx       - every sale (one row per product line)
    purchases.xlsx   - every purchase (one row per product line)
    stock.xlsx       - current quantity on hand, per product
    party.xlsx       - per-party totals (sales / purchases / combined)
"""

import os
import sys
import platform
import subprocess

from openpyxl import Workbook, load_workbook


# --------------------------------------------------------------------------- #
# Storage location
# --------------------------------------------------------------------------- #
def data_dir() -> str:
    """Folder where the Excel files live.

    When packaged with PyInstaller (``sys.frozen``) we write next to the .exe
    so the files are easy to find; otherwise next to this module.
    """
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    folder = os.path.join(base, "inventory_data")
    os.makedirs(folder, exist_ok=True)
    return folder


DATA_DIR = data_dir()
SALES_FILE = os.path.join(DATA_DIR, "sales.xlsx")
PURCHASES_FILE = os.path.join(DATA_DIR, "purchases.xlsx")
STOCK_FILE = os.path.join(DATA_DIR, "stock.xlsx")
PARTY_FILE = os.path.join(DATA_DIR, "party.xlsx")

TXN_HEADERS = [
    "Date", "Bill No", "PAN No", "Vendor Name", "Vendor Address",
    "Product Name", "Quantity", "Rate", "Amount",
    "ECS", "VAT %", "VAT Amount", "Total",
]
STOCK_HEADERS = ["Product Name", "Quantity"]
PARTY_HEADERS = [
    "PAN No", "Vendor Name", "Vendor Address",
    "Total Sales", "Total Purchases", "Total Combined",
]


# --------------------------------------------------------------------------- #
# Excel helpers
# --------------------------------------------------------------------------- #
def _ensure_file(path: str, headers: list) -> None:
    """Create the workbook with a header row if it does not exist yet."""
    if os.path.exists(path):
        return
    wb = Workbook()
    ws = wb.active
    ws.append(headers)
    wb.save(path)


def _load(path: str, headers: list):
    """Return (workbook, worksheet), creating the file if missing."""
    _ensure_file(path, headers)
    wb = load_workbook(path)
    return wb, wb.active


def num(value) -> float:
    """Parse a value into a float, treating blanks / junk as 0."""
    if value is None:
        return 0.0
    try:
        return float(str(value).replace(",", "").strip() or 0)
    except ValueError:
        return 0.0


def append_bill(path: str, header: dict, lines: list,
                ecs: float, vat_pct: float, vat_amount: float,
                total: float) -> None:
    """Write a multi-product bill: one row per product line.

    ``header`` has date/bill/pan/vendor/address. ``lines`` is a list of dicts
    with product/qty/rate/amount (amount = qty * rate). Bill-level figures
    (ECS, VAT %, VAT Amount, Total) are repeated on every row so each row is
    self-contained.
    """
    wb, ws = _load(path, TXN_HEADERS)
    for ln in lines:
        ws.append([
            header["date"], header["bill"], header["pan"],
            header["vendor"], header["address"],
            ln["product"], ln["qty"], ln["rate"], ln["amount"],
            ecs, vat_pct, vat_amount, total,
        ])
    wb.save(path)


def product_names() -> list:
    """Distinct product names currently known in stock (for the dropdown)."""
    _ensure_file(STOCK_FILE, STOCK_HEADERS)
    wb = load_workbook(STOCK_FILE)
    ws = wb.active
    names = []
    seen = set()
    for r in range(2, ws.max_row + 1):
        name = ws.cell(row=r, column=1).value
        if name is not None:
            text = str(name).strip()
            key = text.lower()
            if text and key not in seen:
                seen.add(key)
                names.append(text)
    return sorted(names, key=str.lower)


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
            wb.save(STOCK_FILE)
            return new_qty
    new_qty = qty if add else -qty
    ws.append([product.strip(), new_qty])
    wb.save(STOCK_FILE)
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
    """Update a party's running totals. Keyed by PAN (falls back to name)."""
    wb, ws = _load(PARTY_FILE, PARTY_HEADERS)
    key_pan = pan.strip().lower()
    key_name = name.strip().lower()

    def matches(row_pan, row_name) -> bool:
        rp = str(row_pan).strip().lower() if row_pan is not None else ""
        rn = str(row_name).strip().lower() if row_name is not None else ""
        if key_pan:
            return rp == key_pan
        return rn == key_name and rn != ""

    for r in range(2, ws.max_row + 1):
        if matches(ws.cell(row=r, column=1).value,
                   ws.cell(row=r, column=2).value):
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
                ws.cell(row=r, column=2, value=name.strip())
            if address.strip():
                ws.cell(row=r, column=3, value=address.strip())
            wb.save(PARTY_FILE)
            return

    sales = total if is_sale else 0.0
    purch = 0.0 if is_sale else total
    ws.append([pan.strip(), name.strip(), address.strip(),
               sales, purch, sales + purch])
    wb.save(PARTY_FILE)


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


def open_file(path: str, headers: list) -> None:
    """Open a workbook in the OS default app (Excel / Numbers)."""
    _ensure_file(path, headers)
    system = platform.system()
    if system == "Windows":
        os.startfile(path)  # type: ignore[attr-defined]
    elif system == "Darwin":
        subprocess.run(["open", path], check=False)
    else:
        subprocess.run(["xdg-open", path], check=False)
