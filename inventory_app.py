"""
Inventory Management — desktop app (Windows / cross-platform).

GUI layer only. All Excel I/O and business rules live in ``storage.py``.

Layout: a left sidebar navigates between sections that share one window:
    New Bill   - create a sale/purchase bill with one or more products
    Sales      - browse every sale, grouped by bill, with each bill's total
    Purchases  - browse every purchase, grouped by bill, with each bill's total
    Stock      - current quantity on hand per product
    Parties    - per-party Sales / Purchases / Combined totals (searchable)

A bill has header details (PAN, Vendor, Address, Bill No, Date) and one or
more product lines. Each line: Product, Quantity, Rate -> Amount = Qty x Rate.
VAT defaults to 13% of the subtotal; Total = Subtotal + ECS + VAT Amount.
On the New Bill page a side list lets you pick an existing party (auto-fill)
or just type a new one.

Nothing is ever deleted. A mistaken bill is cancelled with Void Bill, which
appends a reversing entry and recomputes stock and party totals from the
ledgers. Those totals can also be rebuilt on demand from the Stock and
Parties pages if they are ever suspected of having drifted.
"""

import os
import sys
import logging
import platform
import traceback
import tkinter as tk
from tkinter import ttk, messagebox
from datetime import date

import storage as db

# --------------------------------------------------------------------------- #
# Look & feel
# --------------------------------------------------------------------------- #
MAC = platform.system() == "Darwin"
FAMILY = "Helvetica Neue" if MAC else "Segoe UI"

F_BASE = (FAMILY, 12)
F_SMALL = (FAMILY, 10)
F_BOLD = (FAMILY, 12, "bold")
F_HEADING = (FAMILY, 22, "bold")
F_NAV = (FAMILY, 13)
F_BRAND = (FAMILY, 17, "bold")

SIDEBAR_BG = "#111827"
SIDEBAR_FG = "#cbd5e1"
NAV_HOVER = "#1f2937"
NAV_ACTIVE = "#2563eb"
CONTENT_BG = "#eef1f5"
CARD_BG = "#ffffff"
GROUP_BG = "#eef2ff"
VOID_FG = "#9ca3af"
ACCENT = "#2563eb"
ACCENT_DK = "#1d4ed8"
GREEN = "#059669"
GREEN_DK = "#047857"
RED = "#dc2626"
AMBER = "#b45309"
TEXT = "#111827"
MUTED = "#6b7280"
BORDER = "#e2e6ec"

DEFAULT_VAT = "13"
RIGHT_COLS = {"Quantity", "Rate", "Amount", "Subtotal", "ECS",
              "VAT %", "VAT Amount", "Total"}
# Columns of the ledger that are meaningful to a person reading the table.
TXN_VIEW = ["Date", "Bill No", "PAN No", "Vendor Name", "Vendor Address",
            "Product Name", "Quantity", "Rate", "Amount",
            "Subtotal", "ECS", "VAT %", "VAT Amount", "Total", "Entered At"]

log = logging.getLogger("inventory")


class InventoryApp(tk.Tk):
    NAV = [("new", "New Bill"), ("sales", "Sales"),
           ("purchases", "Purchases"), ("stock", "Stock"),
           ("parties", "Parties")]

    def __init__(self):
        super().__init__()
        self.title("Inventory Management")
        self.geometry("1200x880")
        self.minsize(1060, 720)
        self.configure(bg=CONTENT_BG)
        self.report_callback_exception = self._on_callback_error
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._init_style()

        # entry-form state
        self.hdr = {k: tk.StringVar() for k in
                    ("pan", "vendor", "address", "bill", "date")}
        self.hdr["date"].set(date.today().strftime("%d/%m/%Y"))
        self.line = {k: tk.StringVar() for k in ("product", "qty", "rate")}
        self.subtotal = tk.StringVar(value="0")
        self.ecs = tk.StringVar(value="0")
        self.vat_pct = tk.StringVar(value=DEFAULT_VAT)
        self.vat_amt = tk.StringVar(value="0")
        self.total = tk.StringVar(value="0")
        self.party_search = tk.StringVar()
        self.lines = []                 # list of {product, qty, rate, amount}
        self._all_parties = []          # cached party rows for the picker
        self._party_view = []           # currently shown (filtered) party rows
        self._saving = False
        self._pending_warning = None    # a warning that must survive clear_form

        self.frames, self.nav_buttons, self.refreshers = {}, {}, {}
        self._build_layout()
        self.party_search.trace_add("write", lambda *_: self._render_party_list())
        self.show("new")

    # ------------------------------------------------------------------ #
    # failure handling
    # ------------------------------------------------------------------ #
    def _on_callback_error(self, exc_type, exc_value, exc_tb) -> None:
        """Catch anything raised inside a Tk callback.

        Without this Tk prints the traceback to stderr and carries on -- and a
        --windowed build has no stderr, so the app would appear to do nothing
        at all when a button failed.
        """
        log.error("unhandled error in a UI action",
                  exc_info=(exc_type, exc_value, exc_tb))
        detail = "".join(traceback.format_exception_only(exc_type, exc_value)).strip()
        messagebox.showerror(
            "Something went wrong",
            f"{detail}\n\nThe details were written to:\n{_log_path()}\n\n"
            f"Your saved data has not been changed by this error.")

    def _on_close(self) -> None:
        if self._saving:
            messagebox.showinfo("Please wait",
                                "A bill is being saved. Try again in a moment.")
            return
        if self.lines and not messagebox.askyesno(
                "Discard bill?",
                f"This bill has {len(self.lines)} unsaved product line(s).\n\n"
                "Close anyway and lose them?"):
            return
        log.info("application closed")
        self.destroy()

    # ------------------------------------------------------------------ #
    # styling
    # ------------------------------------------------------------------ #
    def _init_style(self) -> None:
        st = ttk.Style(self)
        st.theme_use("clam")
        st.configure(".", font=F_BASE)
        st.configure("TEntry", fieldbackground="white", bordercolor=BORDER,
                     lightcolor=BORDER, darkcolor=BORDER, padding=5)
        st.configure("TCombobox", fieldbackground="white", bordercolor=BORDER,
                     lightcolor=BORDER, darkcolor=BORDER, padding=5)
        st.map("TCombobox", fieldbackground=[("readonly", "white")])

        for name, bg, fg, active in (
                ("Primary.TButton", ACCENT, "white", ACCENT_DK),
                ("Success.TButton", GREEN, "white", GREEN_DK),
                ("Ghost.TButton", "#eef2ff", ACCENT, "#e0e7ff")):
            st.configure(name, background=bg, foreground=fg, borderwidth=0,
                         focuscolor=bg, padding=(14, 9),
                         font=F_BOLD if name != "Ghost.TButton" else F_BASE)
            st.map(name, background=[("active", active), ("disabled", "#cbd5e1")],
                   foreground=[("active", fg), ("disabled", "#6b7280")])

        st.configure("Nav.TButton", background=SIDEBAR_BG, foreground=SIDEBAR_FG,
                     borderwidth=0, focuscolor=SIDEBAR_BG, anchor="w",
                     padding=(20, 13), font=F_NAV)
        st.map("Nav.TButton", background=[("active", NAV_HOVER)],
               foreground=[("active", "white")])
        st.configure("NavActive.TButton", background=NAV_ACTIVE,
                     foreground="white", borderwidth=0, focuscolor=NAV_ACTIVE,
                     anchor="w", padding=(20, 13), font=F_NAV)
        st.map("NavActive.TButton", background=[("active", NAV_ACTIVE)],
               foreground=[("active", "white")])

        st.configure("Treeview", background="white", fieldbackground="white",
                     foreground=TEXT, rowheight=28, font=F_BASE, borderwidth=0)
        st.configure("Treeview.Heading", background="#f1f3f7", foreground=MUTED,
                     font=(FAMILY, 11, "bold"), padding=8, borderwidth=0)
        st.map("Treeview", background=[("selected", "#dbeafe")],
               foreground=[("selected", TEXT)])

    # ------------------------------------------------------------------ #
    # layout scaffolding
    # ------------------------------------------------------------------ #
    def _build_layout(self) -> None:
        sidebar = tk.Frame(self, bg=SIDEBAR_BG, width=210)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        tk.Label(sidebar, text="Inventory", bg=SIDEBAR_BG, fg="white",
                 font=F_BRAND).pack(anchor="w", padx=22, pady=(26, 0))
        tk.Label(sidebar, text="MANAGEMENT", bg=SIDEBAR_BG, fg="#64748b",
                 font=(FAMILY, 10, "bold")).pack(anchor="w", padx=22, pady=(0, 22))
        for key, label in self.NAV:
            btn = ttk.Button(sidebar, text=label, style="Nav.TButton",
                             takefocus=False, command=lambda k=key: self.show(k))
            btn.pack(fill="x")
            self.nav_buttons[key] = btn
        tk.Label(sidebar, text="VAT 13% default", bg=SIDEBAR_BG, fg="#475569",
                 font=F_SMALL).pack(side="bottom", anchor="w", padx=22, pady=16)

        self.container = tk.Frame(self, bg=CONTENT_BG)
        self.container.pack(side="right", fill="both", expand=True)
        self.container.rowconfigure(0, weight=1)
        self.container.columnconfigure(0, weight=1)

        self.frames["new"] = self._build_new()
        self.frames["sales"] = self._build_txn("sales", "Sales", True)
        self.frames["purchases"] = self._build_txn("purchases", "Purchases", False)
        self.frames["stock"] = self._build_stock()
        self.frames["parties"] = self._build_parties()
        for f in self.frames.values():
            f.grid(row=0, column=0, sticky="nsew")

    def show(self, key: str) -> None:
        self.frames[key].tkraise()
        for k, btn in self.nav_buttons.items():
            btn.configure(style="NavActive.TButton" if k == key else "Nav.TButton")
        if key == "new":
            self._refresh_products()
            self._refresh_parties_picker()
        elif key in self.refreshers:
            self.refreshers[key]()

    def _page(self, title: str, subtitle: str = "") -> tk.Frame:
        frame = tk.Frame(self.container, bg=CONTENT_BG)
        bar = tk.Frame(frame, bg=CONTENT_BG)
        bar.pack(fill="x", padx=28, pady=(22, 6))
        tk.Label(bar, text=title, bg=CONTENT_BG, fg=TEXT,
                 font=F_HEADING).pack(side="left")
        if subtitle:
            tk.Label(bar, text=subtitle, bg=CONTENT_BG, fg=MUTED,
                     font=F_SMALL).pack(side="left", padx=12, pady=(14, 0))
        return frame

    def _card(self, parent, title=None, fill="x", expand=False) -> tk.Frame:
        outer = tk.Frame(parent, bg=CONTENT_BG)
        outer.pack(fill="both" if expand else fill, expand=expand, padx=28, pady=7)
        if title:
            tk.Label(outer, text=title.upper(), bg=CONTENT_BG, fg=MUTED,
                     font=(FAMILY, 10, "bold")).pack(anchor="w", pady=(0, 5))
        shell = tk.Frame(outer, bg=CARD_BG, highlightbackground=BORDER,
                         highlightthickness=1, bd=0)
        shell.pack(fill="both", expand=True)
        inner = tk.Frame(shell, bg=CARD_BG)
        inner.pack(fill="both", expand=True, padx=18, pady=14)
        return inner

    @staticmethod
    def _flabel(parent, text, r, c, bold=False) -> None:
        tk.Label(parent, text=text, bg=CARD_BG, fg=TEXT,
                 font=F_BOLD if bold else F_BASE, anchor="e").grid(
            row=r, column=c, sticky="e", padx=(6, 8), pady=7)

    # ------------------------------------------------------------------ #
    # NEW BILL page
    # ------------------------------------------------------------------ #
    def _build_new(self) -> tk.Frame:
        page = self._page("New Bill",
                          "Pick a party or type one, add products, then save.")

        # status + actions pinned to the bottom FIRST so the expanding
        # Products card below can never push them off-screen.
        self.status = tk.Label(page, text="Ready.", bg=CONTENT_BG, fg=GREEN,
                               font=F_BASE, anchor="w", justify="left",
                               wraplength=980)
        self.status.pack(side="bottom", anchor="w", padx=28, pady=(2, 12))
        act = tk.Frame(page, bg=CONTENT_BG)
        act.pack(side="bottom", fill="x", padx=28, pady=(6, 4))
        self.btn_sale = ttk.Button(act, text="SAVE AS SALE", style="Success.TButton",
                                   command=self.save_sale)
        self.btn_sale.pack(side="left", padx=(0, 10))
        self.btn_purchase = ttk.Button(act, text="SAVE AS PURCHASE",
                                       style="Primary.TButton",
                                       command=self.save_purchase)
        self.btn_purchase.pack(side="left", padx=(0, 10))
        self.btn_clear = ttk.Button(act, text="Clear Bill", style="Ghost.TButton",
                                    command=self.clear_form)
        self.btn_clear.pack(side="left")

        # --- bill details + party picker (side by side) ---
        bd = self._card(page, "Bill Details")
        bd.columnconfigure(4, weight=1)
        self._flabel(bd, "PAN No:", 0, 0)
        ttk.Entry(bd, textvariable=self.hdr["pan"], width=24).grid(
            row=0, column=1, sticky="w", pady=7)
        self._flabel(bd, "Bill No:", 0, 2)
        ttk.Entry(bd, textvariable=self.hdr["bill"], width=24).grid(
            row=0, column=3, sticky="w", pady=7)
        self._flabel(bd, "Vendor Name:", 1, 0)
        ttk.Entry(bd, textvariable=self.hdr["vendor"], width=24).grid(
            row=1, column=1, sticky="w", pady=7)
        self._flabel(bd, "Date:", 1, 2)
        ttk.Entry(bd, textvariable=self.hdr["date"], width=24).grid(
            row=1, column=3, sticky="w", pady=7)
        self._flabel(bd, "Vendor Address:", 2, 0)
        ttk.Entry(bd, textvariable=self.hdr["address"], width=58).grid(
            row=2, column=1, columnspan=3, sticky="w", pady=7)

        panel = tk.Frame(bd, bg=CARD_BG, highlightbackground=BORDER,
                         highlightthickness=1)
        panel.grid(row=0, column=4, rowspan=3, sticky="nsew", padx=(24, 0))
        tk.Label(panel, text="Choose Party", bg=CARD_BG, fg=TEXT,
                 font=F_BOLD).pack(anchor="w", padx=10, pady=(8, 2))
        ttk.Entry(panel, textvariable=self.party_search, width=26).pack(
            fill="x", padx=10)
        lbwrap = tk.Frame(panel, bg=CARD_BG)
        lbwrap.pack(fill="both", expand=True, padx=10, pady=(6, 4))
        self.party_lb = tk.Listbox(lbwrap, height=5, activestyle="none",
                                   bg="white", fg=TEXT, font=F_SMALL, bd=0,
                                   highlightthickness=1, highlightbackground=BORDER,
                                   exportselection=False)
        plsb = ttk.Scrollbar(lbwrap, orient="vertical",
                             command=self.party_lb.yview)
        self.party_lb.configure(yscrollcommand=plsb.set)
        self.party_lb.pack(side="left", fill="both", expand=True)
        plsb.pack(side="right", fill="y")
        self.party_lb.bind("<<ListboxSelect>>", self._on_party_pick)
        tk.Label(panel, text="Pick to autofill — or type new details to add one.",
                 bg=CARD_BG, fg=MUTED, font=(FAMILY, 9)).pack(anchor="w",
                                                              padx=10, pady=(0, 8))

        # --- products ---
        pr = self._card(page, "Products", fill="both", expand=True)
        row = tk.Frame(pr, bg=CARD_BG)
        row.pack(fill="x")
        tk.Label(row, text="Product Name:", bg=CARD_BG, fg=TEXT,
                 font=F_BASE).pack(side="left", padx=(0, 6))
        self.product_cb = ttk.Combobox(row, textvariable=self.line["product"],
                                       width=24, values=[])
        self.product_cb.pack(side="left", padx=(0, 14))
        tk.Label(row, text="Quantity:", bg=CARD_BG, fg=TEXT,
                 font=F_BASE).pack(side="left", padx=(0, 6))
        qe = ttk.Entry(row, textvariable=self.line["qty"], width=8)
        qe.pack(side="left", padx=(0, 14))
        tk.Label(row, text="Rate:", bg=CARD_BG, fg=TEXT,
                 font=F_BASE).pack(side="left", padx=(0, 6))
        re = ttk.Entry(row, textvariable=self.line["rate"], width=10)
        re.pack(side="left", padx=(0, 14))
        ttk.Button(row, text="+ Add Product", style="Ghost.TButton",
                   command=self.add_product).pack(side="left")
        for w in (self.product_cb, qe, re):
            w.bind("<Return>", lambda e: self.add_product())

        cols = ("Product Name", "Quantity", "Rate", "Amount")
        tbl = tk.Frame(pr, bg=CARD_BG)
        tbl.pack(fill="both", expand=True, pady=(12, 6))
        self.items = ttk.Treeview(tbl, columns=cols, show="headings", height=5)
        for c, w, a in (("Product Name", 300, "w"), ("Quantity", 110, "e"),
                        ("Rate", 110, "e"), ("Amount", 150, "e")):
            self.items.heading(c, text=c)
            self.items.column(c, width=w, anchor=a, stretch=(c == "Product Name"))
        ysb = ttk.Scrollbar(tbl, orient="vertical", command=self.items.yview)
        self.items.configure(yscrollcommand=ysb.set)
        self.items.pack(side="left", fill="both", expand=True)
        ysb.pack(side="right", fill="y")
        ttk.Button(pr, text="Remove Selected", style="Ghost.TButton",
                   command=self.remove_selected).pack(anchor="w")

        # --- totals ---
        tt = self._card(page, "Totals")
        self._flabel(tt, "Subtotal:", 0, 0)
        ttk.Entry(tt, textvariable=self.subtotal, width=16,
                  state="readonly").grid(row=0, column=1, sticky="w", pady=7)
        self._flabel(tt, "ECS:", 0, 2)
        ee = ttk.Entry(tt, textvariable=self.ecs, width=16)
        ee.grid(row=0, column=3, sticky="w", pady=7)
        self._flabel(tt, "VAT %:", 1, 0)
        ve = ttk.Entry(tt, textvariable=self.vat_pct, width=16)
        ve.grid(row=1, column=1, sticky="w", pady=7)
        self._flabel(tt, "VAT Amount:", 1, 2)
        ttk.Entry(tt, textvariable=self.vat_amt, width=16,
                  state="readonly").grid(row=1, column=3, sticky="w", pady=7)
        self._flabel(tt, "Total:", 2, 0, bold=True)
        ttk.Entry(tt, textvariable=self.total, width=16, font=F_BOLD,
                  state="readonly").grid(row=2, column=1, sticky="w", pady=7)
        tk.Label(tt, text="Total = Subtotal + ECS + VAT — calculated for you.",
                 bg=CARD_BG, fg=MUTED, font=F_SMALL).grid(
            row=2, column=2, columnspan=2, sticky="w", padx=8)
        for w in (ee, ve):
            w.bind("<KeyRelease>", lambda e: self.calculate_total())
        return page

    # -- party picker --------------------------------------------------- #
    def _refresh_parties_picker(self) -> None:
        if not hasattr(self, "party_lb"):
            return
        try:
            self._all_parties = db.read_rows(db.PARTY_FILE, db.PARTY_HEADERS)
        except db.StorageError as exc:
            # Must not fail silently: an empty picker looks like "no parties
            # yet", so the user retypes one and creates a duplicate row.
            self._all_parties = []
            log.warning("party picker could not load: %s", exc)
            self._warn(f"Party list unavailable — {exc} "
                       f"Pick nothing and check the Parties page.")
        self._render_party_list()

    def _render_party_list(self) -> None:
        if not hasattr(self, "party_lb"):
            return
        q = self.party_search.get().strip().lower()
        self.party_lb.delete(0, "end")
        self._party_view = []
        for r in self._all_parties:
            pan, name = str(r[db.P_PAN] or ""), str(r[db.P_NAME] or "")
            if not q or q in name.lower() or q in pan.lower():
                self._party_view.append(r)
                label = name or pan or "(unnamed)"
                if pan:
                    label += f"   ·   {pan}"
                self.party_lb.insert("end", label)

    def _on_party_pick(self, _event=None) -> None:
        sel = self.party_lb.curselection()
        if not sel:
            return
        r = self._party_view[sel[0]]
        self.hdr["pan"].set(r[db.P_PAN] or "")
        self.hdr["vendor"].set(r[db.P_NAME] or "")
        self.hdr["address"].set(r[db.P_ADDR] or "")

    def _refresh_products(self) -> None:
        if not hasattr(self, "product_cb"):
            return
        try:
            self.product_cb["values"] = db.product_names()
        except db.StorageError as exc:
            log.warning("product list could not load: %s", exc)
            self._warn(f"Product list unavailable — {exc} "
                       f"You can still type the product name.")

    # -- line items ----------------------------------------------------- #
    def add_product(self) -> bool:
        product = self.line["product"].get().strip()
        qty = db.num(self.line["qty"].get())
        rate = db.num(self.line["rate"].get())
        if not product:
            messagebox.showerror("Missing data", "Enter a Product Name.")
            return False
        if qty <= 0:
            messagebox.showerror("Missing data", "Quantity must be greater than 0.")
            return False
        if rate < 0:
            messagebox.showerror("Invalid rate", "Rate cannot be negative.")
            return False
        if rate == 0 and not messagebox.askyesno(
                "Zero rate", "Rate is 0 (or blank) — this adds quantity to stock "
                "for no money. Add this line anyway?"):
            return False
        try:
            amount = db.line_amount(qty, rate)
        except ValueError as exc:
            messagebox.showerror("Number too large", str(exc))
            return False
        self.lines.append({"product": product, "qty": qty,
                           "rate": rate, "amount": amount})
        self.items.insert("", "end", values=(
            product, self._fmt(qty), self._fmt(rate), self._fmt(amount)))
        for k in self.line:
            self.line[k].set("")
        self.calculate_total()
        self.product_cb.focus_set()
        return True

    def remove_selected(self) -> None:
        for item in self.items.selection():
            idx = self.items.index(item)
            self.items.delete(item)
            del self.lines[idx]
        self.calculate_total()

    # -- totals --------------------------------------------------------- #
    def _totals(self) -> dict:
        """Bill figures from storage — the single definition of the math."""
        return db.bill_totals(self.lines, db.num(self.ecs.get()),
                              db.num(self.vat_pct.get()))

    def calculate_total(self) -> None:
        try:
            t = self._totals()
        except ValueError as exc:
            self._warn(str(exc))
            return
        self.subtotal.set(self._fmt(t["subtotal"]))
        self.vat_amt.set(self._fmt(t["vat_amount"]))
        self.total.set(self._fmt(t["total"]))

    @staticmethod
    def _fmt(value) -> str:
        try:
            value = float(value)
        except (TypeError, ValueError):
            return "0"
        if value != value or value in (float("inf"), float("-inf")):
            return "0"
        if value == int(value):
            return str(int(value))
        return f"{value:.2f}"

    # -- save ----------------------------------------------------------- #
    def _gather(self):
        # A product typed but not yet added is part of the bill the user
        # believes they are saving. Push it through the same validation the
        # Add Product button uses rather than discarding it silently.
        if self.line["product"].get().strip():
            if not self.add_product():
                self._warn("Not saved — finish or clear the product line above.")
                return None
        if not self.lines:
            messagebox.showerror("No products",
                                 "Add at least one product to the bill.")
            self._warn("Not saved — the bill has no products.")
            return None
        if not self.hdr["pan"].get().strip() and not self.hdr["vendor"].get().strip():
            messagebox.showerror("Missing party",
                                 "Enter a PAN No or Vendor Name (or pick a party).")
            self._warn("Not saved — enter a PAN No or Vendor Name.")
            return None
        if not self.vat_pct.get().strip():
            self.vat_pct.set(DEFAULT_VAT)
        self.calculate_total()
        return {
            "date": self.hdr["date"].get().strip(),
            "bill": self.hdr["bill"].get().strip(),
            "pan": self.hdr["pan"].get().strip(),
            "vendor": self.hdr["vendor"].get().strip(),
            "address": self.hdr["address"].get().strip(),
        }

    def save_sale(self) -> None:
        header = self._gather()
        if header is None:
            return
        try:
            short = db.shortages(self.lines)
        except db.StorageError as exc:
            self._fail(f"Could not check stock levels — {exc} Nothing was saved.")
            return
        if short and not messagebox.askyesno(
                "Low stock", "These products will go negative:\n\n"
                + "\n".join(f"  {s['product']}: have {self._fmt(s['have'])}, "
                            f"selling {self._fmt(s['selling'])}" for s in short)
                + "\n\nSave anyway?"):
            self._warn("Not saved — you chose not to sell below stock.")
            return
        self._do_save(is_sale=True, header=header, label="Sale")

    def save_purchase(self) -> None:
        header = self._gather()
        if header is None:
            return
        self._do_save(is_sale=False, header=header, label="Purchase")

    def _do_save(self, is_sale: bool, header: dict, label: str) -> None:
        if self._saving:
            return
        ledger = db.SALES_FILE if is_sale else db.PURCHASES_FILE
        try:
            duplicate = db.bill_exists(ledger, header["bill"])
        except db.StorageError as exc:
            log.warning("duplicate-bill check failed: %s", exc)
            if not messagebox.askyesno(
                    "Could not check for duplicates",
                    f"{exc}\n\nThe app could not confirm whether Bill No "
                    f"{header['bill']} has already been entered.\n\nSave anyway?"):
                self._warn("Not saved — the duplicate check could not run.")
                return
            duplicate = False
        if duplicate and not messagebox.askyesno(
                "Duplicate bill", f"Bill No {header['bill']} already exists in "
                "this ledger.\n\nSave it again anyway?"):
            self._warn(f"Not saved — Bill No {header['bill']} already exists.")
            return

        count = len(self.lines)
        totals, saved, error = None, None, None
        self._set_saving(True)
        try:
            totals = self._totals()
            saved = db.commit_bill(is_sale, header, self.lines, totals)
        except Exception as exc:  # noqa: BLE001 - dispatched below
            error = exc
        finally:
            # Always restore the buttons and the flag BEFORE any dialog runs,
            # so a close attempt during the error modal is not told that a save
            # is still in progress.
            self._set_saving(False)

        if error is not None:
            self._report_save_error(error)
            return

        self._ok(f"{label} saved as Bill {saved['bill_no']} — {count} "
                 f"product(s), total {self._fmt(totals['total'])}.")

    def _report_save_error(self, exc: Exception) -> None:
        """Turn a commit failure into an accurate message.

        The distinction that matters: an interrupted swap means the bill may
        ALREADY be in the ledger, so telling the user nothing was saved would
        invite them to enter it twice.
        """
        if isinstance(exc, db.CommitInterruptedError):
            log.error("commit interrupted: %s", exc)
            msg = (f"Save interrupted — {exc} Do NOT re-enter this bill. "
                   f"Close and reopen the app; it finishes the pending save on "
                   f"startup.")
            self.status.configure(text=msg, fg=RED)
            messagebox.showerror("Save interrupted", msg)
            return
        if isinstance(exc, db.FileLockedError):
            self._fail(f"Not saved — {exc} Close it and try again. "
                       f"Nothing was changed.")
            return
        if isinstance(exc, db.DataIntegrityError):
            self._fail(f"Not saved — {exc} Nothing was changed.")
            return
        if isinstance(exc, db.StorageError):
            log.error("save failed", exc_info=exc)
            self._fail(f"Not saved — {exc}")
            return
        if isinstance(exc, ValueError):
            self._fail(f"Not saved — {exc}")
            return
        # exc_info=exc, not log.exception(): this runs outside the except
        # block, where the ambient exception state is already cleared.
        log.error("unexpected error while saving", exc_info=exc)
        self._fail(f"Not saved — unexpected error: {exc} "
                   f"The details are in {_log_path()}.")

    def _set_saving(self, on: bool) -> None:
        self._saving = on
        state = "disabled" if on else "normal"
        for btn in (self.btn_sale, self.btn_purchase, self.btn_clear):
            btn.configure(state=state)
        if on:
            self.status.configure(text="Saving — please wait…", fg=ACCENT)
        elif str(self.status.cget("text")).startswith("Saving"):
            # Never leave "Saving…" on screen once the attempt has finished.
            self.status.configure(text="Ready.", fg=MUTED)
        self.update_idletasks()

    def _ok(self, msg: str) -> None:
        log.info("%s", msg)
        self._pending_warning = None
        self.clear_form()
        if self._pending_warning:
            # clear_form reloads the pickers; if that failed, say so alongside
            # the success rather than overwriting it with green text.
            self.status.configure(text=f"{msg}   ⚠ {self._pending_warning}",
                                  fg=AMBER)
        else:
            self.status.configure(text=msg, fg=GREEN)

    def _warn(self, msg: str) -> None:
        self._pending_warning = msg
        self.status.configure(text=msg, fg=AMBER)

    def _fail(self, msg: str) -> None:
        log.error("%s", msg)
        self.status.configure(text=msg, fg=RED)
        messagebox.showerror("Not saved", msg)

    def clear_form(self) -> None:
        for v in self.hdr.values():
            v.set("")
        self.hdr["date"].set(date.today().strftime("%d/%m/%Y"))
        for v in self.line.values():
            v.set("")
        self.items.delete(*self.items.get_children())
        self.lines = []
        self.subtotal.set("0")
        self.ecs.set("0")
        self.vat_pct.set(DEFAULT_VAT)
        self.vat_amt.set("0")
        self.total.set("0")
        self._pending_warning = None
        self.status.configure(text="Ready.", fg=GREEN)
        self._refresh_products()
        self._refresh_parties_picker()

    # ------------------------------------------------------------------ #
    # SALES / PURCHASES — grouped by bill
    # ------------------------------------------------------------------ #
    def _build_txn(self, key: str, title: str, is_sale: bool) -> tk.Frame:
        page = self._page(title,
                          "Grouped by bill — select a bill to cancel it.")
        body = self._card(page, fill="both", expand=True)
        wrap = tk.Frame(body, bg=CARD_BG)
        wrap.pack(fill="both", expand=True)
        tree = ttk.Treeview(wrap, columns=TXN_VIEW, show="tree headings")
        tree.heading("#0", text="Bill")
        tree.column("#0", width=130, anchor="w", stretch=False)
        for h in TXN_VIEW:
            tree.heading(h, text=h)
            tree.column(h, width=92, anchor="e" if h in RIGHT_COLS else "w",
                        stretch=False)
        tree.tag_configure("bill", background=GROUP_BG,
                           font=(FAMILY, 11, "bold"))
        tree.tag_configure("void", foreground=VOID_FG)
        ysb = ttk.Scrollbar(wrap, orient="vertical", command=tree.yview)
        xsb = ttk.Scrollbar(wrap, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)
        tree.grid(row=0, column=0, sticky="nsew")
        ysb.grid(row=0, column=1, sticky="ns")
        xsb.grid(row=1, column=0, sticky="ew")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)

        grand = tk.StringVar(value="")
        node_bill = {}          # tree item id -> (bill_id, human description)

        def refresh() -> None:
            tree.delete(*tree.get_children())
            node_bill.clear()
            path = db.SALES_FILE if is_sale else db.PURCHASES_FILE
            try:
                found = db.bills(path)
            except db.StorageError as exc:
                messagebox.showerror("Could not read the ledger", str(exc))
                grand.set("Grand total: unavailable")
                return
            total, orphans = 0.0, 0
            for bill in found:
                h, t = bill["header"], bill["totals"]
                cancelled = bool(bill["voided_by"]) or bool(bill["voids"])
                head_vals = [h["date"], h["bill"], h["pan"], h["vendor"],
                             h["address"], "", "", "", "",
                             t["subtotal"], t["ecs"], t["vat_pct"],
                             t["vat_amount"], t["total"], h["entered"]]
                label = str(h["bill"] or "—")
                if bill["synthetic"]:
                    label += "  (no Bill ID)"
                    orphans += 1
                elif bill["voided_by"]:
                    label += "  (cancelled)"
                elif bill["voids"]:
                    label += "  (cancels)"
                # Identify the bill the way the user sees it, not by the
                # internal Bill ID, which appears in no visible column.
                desc = (f"{h['bill'] or '(no Bill No)'} dated "
                        f"{h['date'] or '(no date)'}"
                        f"{' — ' + str(h['vendor']) if h['vendor'] else ''}, "
                        f"total {self._fmt(t['total'])}")
                tags = ("bill", "void") if cancelled else ("bill",)
                pid = tree.insert("", "end", text=label, open=True, tags=tags,
                                  values=[self._cell(v) for v in head_vals])
                node_bill[pid] = (bill["bill_id"], desc)
                for ln in bill["lines"]:
                    line_vals = ["", "", "", "", "", ln["product"], ln["qty"],
                                 ln["rate"], ln["amount"], "", "", "", "", "", ""]
                    cid = tree.insert(pid, "end", text="",
                                      tags=("void",) if cancelled else (),
                                      values=[self._cell(v) for v in line_vals])
                    node_bill[cid] = (bill["bill_id"], desc)
                total += t["total"]
            suffix = (f"    ({orphans} row(s) with no Bill ID)" if orphans else "")
            grand.set(f"Grand total:  {self._fmt(round(total, 2))}{suffix}")
        self.refreshers[key] = refresh

        def void_selected() -> None:
            sel = tree.selection()
            if not sel:
                messagebox.showinfo("Nothing selected",
                                    "Select the bill you want to cancel.")
                return
            bill_id, desc = node_bill.get(sel[0], (None, ""))
            if bill_id is None:
                messagebox.showerror(
                    "Cannot cancel this row",
                    "This row has no Bill ID, so the app cannot tell which "
                    "other rows belong with it. Cancel it by hand in Excel, "
                    "then press Recalculate from Ledgers on the Stock and "
                    "Parties pages.")
                return
            if not messagebox.askyesno(
                    "Cancel this bill?",
                    f"Cancel bill {desc}?\n\nA reversing entry will be added. "
                    f"The original rows stay in the ledger for your records, "
                    f"and stock and party totals are recalculated."):
                return
            path = db.SALES_FILE if is_sale else db.PURCHASES_FILE
            try:
                db.void_bill(is_sale, bill_id)
            except db.RebuildFailedError as exc:
                # The reversal is recorded; only the recalculation failed.
                log.error("void rebuild failed", exc_info=exc)
                messagebox.showwarning(
                    "Cancelled, but totals not recalculated",
                    f"{exc}\n\nDo not cancel it again. Press Recalculate from "
                    f"Ledgers on the Stock and Parties pages once the problem "
                    f"is fixed.")
                refresh()
                return
            except (db.StorageError, ValueError) as exc:
                messagebox.showerror("Could not cancel the bill", str(exc))
                return
            log.info("cancelled bill %s in %s", bill_id, path)
            messagebox.showinfo(
                "Bill cancelled",
                f"Bill {desc} was cancelled.\n\nStock and party totals have "
                f"been recalculated.")
            refresh()

        bar = tk.Frame(page, bg=CONTENT_BG)
        bar.pack(fill="x", padx=28, pady=(2, 16))
        ttk.Button(bar, text="Open in Excel", style="Primary.TButton",
                   command=lambda: self._open_excel(
                       db.SALES_FILE if is_sale else db.PURCHASES_FILE,
                       db.TXN_HEADERS)).pack(side="left", padx=(0, 10))
        ttk.Button(bar, text="Refresh", style="Ghost.TButton",
                   command=refresh).pack(side="left", padx=(0, 10))
        ttk.Button(bar, text="Void Bill", style="Ghost.TButton",
                   command=void_selected).pack(side="left")
        tk.Label(bar, textvariable=grand, bg=CONTENT_BG, fg=TEXT,
                 font=F_BOLD).pack(side="right")
        return page

    # ------------------------------------------------------------------ #
    # STOCK page
    # ------------------------------------------------------------------ #
    def _build_stock(self) -> tk.Frame:
        page = self._page("Stock", "Current quantity on hand.")
        body = self._card(page, fill="both", expand=True)
        tree = self._make_table(body, db.STOCK_HEADERS, width=240,
                                anchors={"Quantity": "e"})

        def refresh() -> None:
            tree.delete(*tree.get_children())
            for r in self._safe_rows(db.STOCK_FILE, db.STOCK_HEADERS):
                tree.insert("", "end", values=[self._cell(v) for v in r])
        self.refreshers["stock"] = refresh

        def rebuild() -> None:
            if not messagebox.askyesno(
                    "Recalculate stock?",
                    "Stock will be recomputed from every sale and purchase in "
                    "the ledgers.\n\nUse this if the quantities look wrong. The "
                    "current file is kept as stock.xlsx.bak.\n\nProceed?"):
                return
            try:
                count = db.rebuild_stock()
            except db.StorageError as exc:
                messagebox.showerror("Could not rebuild stock", str(exc))
                return
            messagebox.showinfo("Stock recalculated",
                                f"{count} product(s) recomputed from the ledgers.")
            refresh()

        bar = tk.Frame(page, bg=CONTENT_BG)
        bar.pack(fill="x", padx=28, pady=(2, 16))
        ttk.Button(bar, text="Open in Excel", style="Primary.TButton",
                   command=lambda: self._open_excel(
                       db.STOCK_FILE, db.STOCK_HEADERS)).pack(side="left",
                                                              padx=(0, 10))
        ttk.Button(bar, text="Refresh", style="Ghost.TButton",
                   command=refresh).pack(side="left", padx=(0, 10))
        ttk.Button(bar, text="Recalculate from Ledgers", style="Ghost.TButton",
                   command=rebuild).pack(side="left")
        return page

    # ------------------------------------------------------------------ #
    # PARTIES page
    # ------------------------------------------------------------------ #
    def _build_parties(self) -> tk.Frame:
        page = self._page("Parties", "Running totals per vendor.")
        sb = tk.Frame(page, bg=CONTENT_BG)
        sb.pack(fill="x", padx=28, pady=(0, 2))
        tk.Label(sb, text="Search (PAN or Name):", bg=CONTENT_BG, fg=TEXT,
                 font=F_BASE).pack(side="left", padx=(0, 8))
        query = tk.StringVar()
        ttk.Entry(sb, textvariable=query, width=32).pack(side="left")

        body = self._card(page, fill="both", expand=True)
        anchors = {h: "e" for h in
                   ("Total Sales", "Total Purchases", "Total Combined")}
        tree = self._make_table(body, db.PARTY_HEADERS, width=130, anchors=anchors)
        store = {"rows": []}

        def render(*_) -> None:
            q = query.get().strip().lower()
            tree.delete(*tree.get_children())
            for r in store["rows"]:
                pan = str(r[db.P_PAN] or "").lower()
                name = str(r[db.P_NAME] or "").lower()
                if not q or q in pan or q in name:
                    tree.insert("", "end", values=[self._cell(v) for v in r])

        def refresh() -> None:
            store["rows"] = self._safe_rows(db.PARTY_FILE, db.PARTY_HEADERS)
            render()
        self.refreshers["parties"] = refresh
        query.trace_add("write", render)

        def rebuild() -> None:
            if not messagebox.askyesno(
                    "Recalculate party totals?",
                    "Every party's totals will be recomputed from the sales and "
                    "purchase ledgers.\n\nThe current file is kept as "
                    "party.xlsx.bak.\n\nProceed?"):
                return
            try:
                count = db.rebuild_party_totals()
            except db.StorageError as exc:
                messagebox.showerror("Could not rebuild party totals", str(exc))
                return
            messagebox.showinfo("Party totals recalculated",
                                f"{count} part(ies) recomputed from the ledgers.")
            refresh()

        bar = tk.Frame(page, bg=CONTENT_BG)
        bar.pack(fill="x", padx=28, pady=(2, 16))
        ttk.Button(bar, text="Open in Excel", style="Primary.TButton",
                   command=lambda: self._open_excel(
                       db.PARTY_FILE, db.PARTY_HEADERS)).pack(side="left",
                                                              padx=(0, 10))
        ttk.Button(bar, text="Refresh", style="Ghost.TButton",
                   command=refresh).pack(side="left", padx=(0, 10))
        ttk.Button(bar, text="Recalculate from Ledgers", style="Ghost.TButton",
                   command=rebuild).pack(side="left")
        return page

    # ------------------------------------------------------------------ #
    # shared helpers
    # ------------------------------------------------------------------ #
    def _make_table(self, parent, headers, width=100, anchors=None) -> ttk.Treeview:
        anchors = anchors or {}
        wrap = tk.Frame(parent, bg=CARD_BG)
        wrap.pack(fill="both", expand=True)
        tree = ttk.Treeview(wrap, columns=headers, show="headings")
        for h in headers:
            tree.heading(h, text=h)
            tree.column(h, width=width, anchor=anchors.get(h, "w"), stretch=False)
        ysb = ttk.Scrollbar(wrap, orient="vertical", command=tree.yview)
        xsb = ttk.Scrollbar(wrap, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)
        tree.grid(row=0, column=0, sticky="nsew")
        ysb.grid(row=0, column=1, sticky="ns")
        xsb.grid(row=1, column=0, sticky="ew")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        return tree

    def _safe_rows(self, path: str, headers: list) -> list:
        try:
            return db.read_rows(path, headers)
        except db.StorageError as exc:
            messagebox.showerror("Could not read the file",
                                 f"{os.path.basename(path)}\n\n{exc}")
            return []

    def _open_excel(self, path: str, headers: list) -> None:
        ok, err = db.open_file(path, headers)
        if not ok:
            messagebox.showerror("Could not open file",
                                 f"Failed to open:\n{path}\n\n{err}")

    @staticmethod
    def _cell(value):
        """Render a stored value for a table cell."""
        if value is None:
            return ""
        if isinstance(value, float):
            if value != value or value in (float("inf"), float("-inf")):
                return ""
            if value == int(value):
                return int(value)
            return f"{value:.2f}"       # money never shows 656.9899999999999
        return value


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def _log_path() -> str:
    try:
        return os.path.join(db.data_dir(), "app.log")
    except Exception:  # noqa: BLE001 - only used inside an error message
        return "(the application data folder)"


def _report_startup_failure(exc: BaseException, title: str = None) -> None:
    """Show a startup failure to the user, however little still works.

    A --windowed build has no console, so an unhandled exception here would
    otherwise mean the exe simply never appears.
    """
    try:
        log.error("startup failed", exc_info=exc)
    except Exception:  # noqa: BLE001
        pass
    if title:
        message = str(exc)
    else:
        detail = "".join(
            traceback.format_exception_only(type(exc), exc)).strip()
        message = (f"Inventory Management could not start.\n\n{detail}\n\n"
                   f"If this mentions a folder or permission, check the "
                   f"INVENTORY_DATA_DIR setting or the app data folder.")
    try:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(title or "Inventory Management", message)
        root.destroy()
    except Exception:  # noqa: BLE001 - no display at all
        print(message, file=sys.stderr)


def _startup_notices() -> list:
    """Do the once-per-launch housekeeping. Returns messages for the user."""
    notices = []

    # setup_logging first, so the legacy migration's own log lines are kept.
    db.setup_logging()
    log.info("starting up (data dir: %s)", db.data_dir())
    db.acquire_single_instance_lock()

    result = db.recover()
    if result["message"]:
        notices.append(result["message"])
    elif result["recovered"]:
        log.warning("an interrupted save was recovered on startup")
        notices.append("A save that had been interrupted was completed, and "
                       "stock and party totals were recalculated.")

    if db.legacy_data_was_migrated():
        # Files written by the previous version can carry duplicate product or
        # party rows from a matching bug it had; the rebuild is what heals them.
        log.info("legacy data migrated -- rebuilding derived totals once")
        try:
            db.rebuild_stock()
            db.rebuild_party_totals()
            notices.append(
                f"Your existing data was copied into the app's data folder:\n"
                f"{db.data_dir()}\n\nStock and party totals were recalculated "
                f"from your sales and purchases. The old folder is left as it "
                f"is and is no longer read.")
        except db.StorageError as exc:
            log.error("post-migration rebuild failed: %s", exc)
            notices.append(f"Your existing data was copied in, but the totals "
                           f"could not be recalculated: {exc} Press Recalculate "
                           f"from Ledgers on the Stock and Parties pages.")

    # Freeze any reconstructed Bill IDs on disk before anything reads them,
    # so they cannot shift if the user re-sorts the sheet in Excel.
    db.migrate_ledgers()
    db.backup_daily()
    return notices


def main() -> int:
    try:
        notices = _startup_notices()
    except db.AlreadyRunningError as exc:
        _report_startup_failure(exc, title="Already running")
        return 1
    except Exception as exc:  # noqa: BLE001
        _report_startup_failure(exc)
        return 1
    try:
        app = InventoryApp()
        for note in notices:
            app.after(150, lambda m=note: messagebox.showinfo(
                "Inventory Management", m))
        app.mainloop()
    except Exception as exc:  # noqa: BLE001
        _report_startup_failure(exc)
        return 1
    finally:
        db.release_single_instance_lock()
    return 0


if __name__ == "__main__":
    sys.exit(main())
