# Inventory Management

A simple Windows desktop app for recording **Sales** and **Purchases**, keeping
a live **Stock** ledger, and tracking running totals per **Party** (vendor).
All data is stored in plain Excel files you can open in Excel any time.

## What it does

Enter the bill details once (PAN No, Vendor Name, Vendor Address, Bill No,
Product Name, Product Quantity, Date, Amount, ECS, VAT, Total), then click a
button:

| Button | What happens |
|--------|--------------|
| **SALES** | Saves the entry to `sales.xlsx`. **Subtracts** the quantity from stock. Adds the Total to that party's *Total Sales*. Warns if stock would go negative. |
| **PURCHASE** | Saves the entry to `purchases.xlsx`. **Adds** the quantity to stock (same product name → quantity increases; new name → new stock row). Adds the Total to that party's *Total Purchases*. |
| **PARTY** | Opens a window listing all parties with their *Total Sales*, *Total Purchases* and *Combined* totals. Has a search box — type a PAN or a name to filter. |
| **STOCK** | Opens a window showing current quantity on hand for every product. |

**Calculate Total** button fills `Total = Amount + ECS + VAT` (you can still
edit it). If Total is left blank when you save, it's calculated automatically.

## Data files

Created automatically in a stable per-user folder, kept **outside** the
project/exe folder so it is never wiped by a rebuild, a re-download, or a
`git` clean/checkpoint restore:

- **macOS:** `~/Library/Application Support/InventoryManagement/inventory_data/`
- **Windows:** `%APPDATA%\InventoryManagement\inventory_data\`
- **Linux:** `~/.local/share/InventoryManagement/inventory_data/`

Override the location with the `INVENTORY_DATA_DIR` environment variable. Data
from an older `inventory_data` folder next to the app is migrated automatically
on first run.

- `sales.xlsx` — every sale
- `purchases.xlsx` — every purchase
- `stock.xlsx` — Product Name + current Quantity
- `party.xlsx` — PAN, Name, Address, Total Sales, Total Purchases, Total Combined

Parties are matched by **PAN No** (and by name when PAN is blank), so the same
vendor's totals keep accumulating across many bills.

## Run from source (any OS with Python + Tk)

```bash
pip install -r requirements.txt
python inventory_app.py
```

## Build the Windows .exe

On a **Windows** machine with Python installed, just double-click
**`build_windows.bat`** (or run it from `cmd`). It produces:

```
dist\InventoryManagement.exe
```

Double-click that `.exe` to run — no Python needed on that machine. The Excel
files are stored in `%APPDATA%\InventoryManagement\inventory_data\` (not beside
the `.exe`), so rebuilding or moving the `.exe` never loses your data.

> The `.exe` must be built on Windows. PyInstaller does not cross-compile from
> macOS/Linux to Windows.

## Project layout

- `inventory_app.py` — the GUI (Tkinter)
- `storage.py` — all Excel reading/writing and business rules (no GUI)
- `requirements.txt` — dependencies
- `build_windows.bat` — one-click Windows build script
