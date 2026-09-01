# Inventory Management

A simple Windows desktop app for recording **Sales** and **Purchases**, keeping
a live **Stock** ledger, and tracking running totals per **Party** (vendor).
All data is stored in plain Excel files you can open in Excel any time.

## What it does

One window with a sidebar. Each section does one job:

| Section | What it's for |
|---------|---------------|
| **New Bill** | Enter a bill: pick or type a party, add one or more product lines, then save it as a sale or a purchase. |
| **Sales** | Every sale, grouped by bill, each showing its product lines and total. Select a bill to cancel it. |
| **Purchases** | The same, for purchases. |
| **Stock** | Current quantity on hand per product, with a **Recalculate from Ledgers** button. |
| **Parties** | Per-vendor *Total Sales*, *Total Purchases* and *Combined*, searchable by PAN or name, with a **Recalculate from Ledgers** button. |

### Entering a bill

Fill in the bill details (PAN No, Vendor Name, Vendor Address, Bill No, Date),
then add product lines one at a time — **Product Name, Quantity, Rate**. Amount
is calculated for you as `Quantity × Rate`, and the totals update as you go:

```
Subtotal   = sum of all line Amounts
VAT Amount = VAT % of Subtotal          (VAT defaults to 13%)
Total      = Subtotal + ECS + VAT Amount
```

Subtotal, VAT Amount and Total are calculated fields — you type ECS and VAT %,
and the rest follows. Then press:

- **SAVE AS SALE** — records the bill in `sales.xlsx`, **subtracts** each
  quantity from stock, and adds the Total to that party's *Total Sales*. If the
  bill would drive any product's stock negative you get a warning first
  (quantities are added up per product, so two lines of the same item count
  together).
- **SAVE AS PURCHASE** — records the bill in `purchases.xlsx`, **adds** each
  quantity to stock, and adds the Total to that party's *Total Purchases*.

If you leave **Bill No** blank the app assigns one — `AUTO-S00007` for a sale,
`AUTO-P00007` for a purchase — so every bill stays individually identifiable.

### Fixing a mistake

Nothing is ever deleted. Select a bill on the Sales or Purchases page and press
**Void Bill**: the original rows stay in the ledger for your records, a
reversing entry is added, and stock and party totals are recalculated. Both
bills are then shown greyed out.

If stock or party totals ever look wrong for any other reason, press
**Recalculate from Ledgers** on the Stock or Parties page. That recomputes them
from every sale and purchase on record, so the sales and purchase ledgers are
always the source of truth.

## Data files

Created automatically in a stable per-user folder, kept **outside** the
project/exe folder so it is never wiped by a rebuild, a re-download, or a
`git` clean/checkpoint restore:

- **macOS:** `~/Library/Application Support/InventoryManagement/inventory_data/`
- **Windows:** `%APPDATA%\InventoryManagement\inventory_data\`
- **Linux:** `~/.local/share/InventoryManagement/inventory_data/`

| File | Contents |
|------|----------|
| `sales.xlsx` | Every sale, one row per product line |
| `purchases.xlsx` | Every purchase, one row per product line |
| `stock.xlsx` | Product Name + current Quantity |
| `party.xlsx` | PAN, Name, Address, Total Sales, Total Purchases, Total Combined |
| `app.log` | What the app did, and any error it hit |
| `backups/YYYY-MM-DD/` | A copy of all four workbooks, taken once a day on first launch |

Parties are matched by **PAN No**. When PAN is blank they are matched by
**Vendor Name + Vendor Address together**, so two different vendors who share a
name are not merged into one.

### Editing the files by hand

You can open these in Excel whenever you like. A few things to know:

- The app reads and writes the sheet named **`Ledger`**. Add your own extra
  sheets freely — the app finds its own by name and leaves the rest alone,
  including when you press Recalculate from Ledgers.
- If you replace a Quantity, Rate, Amount or Total with a **formula**, the app
  refuses to use that cell and tells you exactly which one to fix, rather than
  guessing. Use plain numbers in the columns the app maintains.
- Don't delete the header row, and don't put your own data in columns O, P or Q
  of the sales/purchase sheets — those hold Bill ID, Entered At and Voids Bill
  ID. Keep your own notes on a separate sheet rather than in a spare column
  next to the app's, on any of the four files.
- If the app doesn't recognise a sheet's layout it refuses to **read or write**
  that file rather than overwriting what's there — which also stops
  Recalculate from Ledgers for both books, since totals are derived from the
  sales and purchase ledgers together. Put the header row back (or restore
  `<name>.xlsx.bak`) and it will pick up again.

### Moving the data somewhere else

Set `INVENTORY_DATA_DIR` to any folder. It is treated as a **base**: the
workbooks land in `<INVENTORY_DATA_DIR>/inventory_data/`. `~` and `%VARS%` are
expanded. Data from an older `inventory_data` folder next to the app is
migrated automatically on first run.

> **One user at a time.** Two copies on the *same* machine are prevented — the
> second one tells you and exits. Across a shared network folder that guard is
> not reliable, so don't point two machines at one folder: the second to save
> wins and the first one's bill is lost.

## How your data is protected

- Every write goes to a temporary file and is swapped into place only once it
  is complete, so an interrupted save can never leave a half-written workbook.
- The previous version of each file is kept alongside it as `<name>.xlsx.bak`.
- A bill touches three files at once. Those swaps are journalled, so if the app
  is interrupted mid-save it finishes the job on its next start and
  recalculates stock and party totals. If it still can't finish — usually
  because a workbook is open in Excel — it says so and keeps the pending save
  for the next attempt rather than discarding it.
- All four workbooks are copied into `backups/<date>/` once a day.
- Only one copy of the app can use a data folder at a time. A second copy says
  so and exits rather than overwriting the first one's work.

While a pending save is outstanding the app saves nothing — no new bills, and
no recalculation — until the workbook it is waiting on is closed and the app is
restarted. It tells you which file that is.

A power cut in the sub-millisecond window between two of those swaps is the one
case the journal can't fully cover on Windows. The books themselves stay
readable; press **Recalculate from Ledgers** if the totals look off.

Your PAN numbers, vendor names and addresses are stored unencrypted in ordinary
Excel files, protected only by your operating system account. Treat the data
folder like a filing cabinet: anyone with access to the machine can read it.

## Run from source (any OS with Python + Tk)

```bash
pip install -r requirements.txt
python inventory_app.py
```

## Running the tests

```bash
pip install -r requirements-dev.txt
pytest -q
```

69 tests covering the storage layer. They always run against a throwaway
folder and refuse to start if they are ever pointed at a real data directory.

`pytest.ini` scopes this to the legacy app's suite only. The rewrite in
`inventory_system/` has its own dependencies and is run from inside that
directory:

```bash
cd inventory_system && pytest -q
```

## Build the Windows .exe

On a **Windows** machine with Python installed, just double-click
**`build_windows.bat`** (or run it from `cmd`). It installs what it needs,
builds, and stops with a clear error if anything fails. It produces:

```
dist\InventoryManagement.exe
```

Double-click that `.exe` to run — no Python needed on that machine. The Excel
files are stored in `%APPDATA%\InventoryManagement\inventory_data\` (not beside
the `.exe`), so rebuilding or moving the `.exe` never loses your data.

> The `.exe` must be built on Windows. PyInstaller does not cross-compile from
> macOS/Linux to Windows.

**If the app won't start**, open
`%APPDATA%\InventoryManagement\inventory_data\app.log` — the reason is in
there. The app also shows an error dialog rather than closing silently.

## Project layout

- `inventory_app.py` — the GUI (Tkinter), and nothing else
- `storage.py` — all Excel reading/writing, the bill arithmetic, and the
  durability guarantees (no GUI, fully testable)
- `test_storage.py` / `conftest.py` — the test suite
- `requirements.txt` / `requirements-dev.txt` — dependencies
- `build_windows.bat` — one-click Windows build script

## Upgrade in progress: `inventory_system/`

A production-grade rewrite (PySide6 + SQLAlchemy + PostgreSQL) is being
built in [`inventory_system/`](inventory_system/), alongside this app, and
will replace it once feature-complete. **This app is unaffected and is
still what real users should run today.** See
[`inventory_system/docs/architecture.md`](inventory_system/docs/architecture.md)
for the design and phased migration plan.
