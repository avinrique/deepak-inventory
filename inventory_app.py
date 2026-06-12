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
"""

import os
import platform
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
ACCENT = "#2563eb"
ACCENT_DK = "#1d4ed8"
GREEN = "#059669"
GREEN_DK = "#047857"
RED = "#dc2626"
TEXT = "#111827"
MUTED = "#6b7280"
BORDER = "#e2e6ec"

DEFAULT_VAT = "13"
RIGHT_COLS = {"Quantity", "Rate", "Amount", "Subtotal", "ECS",
              "VAT %", "VAT Amount", "Total"}


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

        self.frames, self.nav_buttons, self.refreshers = {}, {}, {}
        self._build_layout()
        self.party_search.trace_add("write", lambda *_: self._render_party_list())
        self.show("new")

    # ------------------------------------------------------------------ #
    # styling
    # ------------------------------------------------------------------ #
    def _init_style(self):
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
    def _build_layout(self):
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
        self.frames["sales"] = self._build_txn("sales", "Sales", db.SALES_FILE)
        self.frames["purchases"] = self._build_txn(
            "purchases", "Purchases", db.PURCHASES_FILE)
        self.frames["stock"] = self._build_stock()
        self.frames["parties"] = self._build_parties()
        for f in self.frames.values():
            f.grid(row=0, column=0, sticky="nsew")

    def show(self, key):
        self.frames[key].tkraise()
        for k, btn in self.nav_buttons.items():
            btn.configure(style="NavActive.TButton" if k == key else "Nav.TButton")
        if key == "new":
            self._refresh_products()
            self._refresh_parties_picker()
        elif key in self.refreshers:
            self.refreshers[key]()

    def _page(self, key, title, subtitle=""):
        frame = tk.Frame(self.container, bg=CONTENT_BG)
        bar = tk.Frame(frame, bg=CONTENT_BG)
        bar.pack(fill="x", padx=28, pady=(22, 6))
        tk.Label(bar, text=title, bg=CONTENT_BG, fg=TEXT,
                 font=F_HEADING).pack(side="left")
        if subtitle:
            tk.Label(bar, text=subtitle, bg=CONTENT_BG, fg=MUTED,
                     font=F_SMALL).pack(side="left", padx=12, pady=(14, 0))
        return frame

    def _card(self, parent, title=None, fill="x", expand=False):
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
    def _flabel(parent, text, r, c, bold=False):
        tk.Label(parent, text=text, bg=CARD_BG, fg=TEXT,
                 font=F_BOLD if bold else F_BASE, anchor="e").grid(
            row=r, column=c, sticky="e", padx=(6, 8), pady=7)

    # ------------------------------------------------------------------ #
    # NEW BILL page
    # ------------------------------------------------------------------ #
    def _build_new(self):
        page = self._page("new", "New Bill",
                          "Pick a party or type one, add products, then save.")

        # status + actions pinned to the bottom FIRST so the expanding
        # Products card below can never push them off-screen.
        self.status = tk.Label(page, text="Ready.", bg=CONTENT_BG, fg=GREEN,
                               font=F_BASE)
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
        ttk.Button(act, text="Clear Bill", style="Ghost.TButton",
                   command=self.clear_form).pack(side="left")

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
        ttk.Button(tt, text="Calculate Total", style="Ghost.TButton",
                   command=self.calculate_total).grid(
            row=2, column=2, columnspan=2, sticky="w", padx=8)
        for w in (ee, ve):
            w.bind("<KeyRelease>", lambda e: self.calculate_total())
        return page

    # -- party picker --------------------------------------------------- #
    def _refresh_parties_picker(self):
        if not hasattr(self, "party_lb"):
            return
        try:
            self._all_parties = db.read_rows(db.PARTY_FILE, db.PARTY_HEADERS)
        except Exception:
            self._all_parties = []
        self._render_party_list()

    def _render_party_list(self):
        if not hasattr(self, "party_lb"):
            return
        q = self.party_search.get().strip().lower()
        self.party_lb.delete(0, "end")
        self._party_view = []
        for r in self._all_parties:
            pan, name = str(r[0] or ""), str(r[1] or "")
            if not q or q in name.lower() or q in pan.lower():
                self._party_view.append(r)
                label = name or pan or "(unnamed)"
                if pan:
                    label += f"   ·   {pan}"
                self.party_lb.insert("end", label)

    def _on_party_pick(self, _event=None):
        sel = self.party_lb.curselection()
        if not sel:
            return
        r = self._party_view[sel[0]]
        self.hdr["pan"].set(r[0] or "")
        self.hdr["vendor"].set(r[1] or "")
        self.hdr["address"].set(r[2] or "")

    def _refresh_products(self):
        if not hasattr(self, "product_cb"):
            return
        try:
            self.product_cb["values"] = db.product_names()
        except Exception:
            pass  # keep last list if stock.xlsx is momentarily locked

    # -- line items ----------------------------------------------------- #
    def add_product(self):
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
        amount = round(qty * rate, 2)
        self.lines.append({"product": product, "qty": qty,
                           "rate": rate, "amount": amount})
        self.items.insert("", "end", values=(
            product, self._fmt(qty), self._fmt(rate), self._fmt(amount)))
        for k in self.line:
            self.line[k].set("")
        self.calculate_total()
        self.product_cb.focus_set()
        return True

    def remove_selected(self):
        for item in self.items.selection():
            idx = self.items.index(item)
            self.items.delete(item)
            del self.lines[idx]
        self.calculate_total()

    # -- totals --------------------------------------------------------- #
    def calculate_total(self):
        subtotal = round(sum(ln["amount"] for ln in self.lines), 2)
        vat_amount = round(subtotal * db.num(self.vat_pct.get()) / 100.0, 2)
        total = round(subtotal + db.num(self.ecs.get()) + vat_amount, 2)
        self.subtotal.set(self._fmt(subtotal))
        self.vat_amt.set(self._fmt(vat_amount))
        self.total.set(self._fmt(total))

    @staticmethod
    def _fmt(value):
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
        if self.line["product"].get().strip() and db.num(self.line["qty"].get()) > 0:
            if not self.add_product():
                return None
        if not self.lines:
            messagebox.showerror("No products",
                                 "Add at least one product to the bill.")
            return None
        if not self.hdr["pan"].get().strip() and not self.hdr["vendor"].get().strip():
            messagebox.showerror("Missing party",
                                 "Enter a PAN No or Vendor Name (or pick a party).")
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

    def save_sale(self):
        header = self._gather()
        if header is None:
            return
        short = []
        for ln in self.lines:
            have = db.stock_on_hand(ln["product"])
            if ln["qty"] > have:
                short.append(f"  {ln['product']}: have {self._fmt(have)}, "
                             f"selling {self._fmt(ln['qty'])}")
        if short and not messagebox.askyesno(
                "Low stock", "These products will go negative:\n\n"
                + "\n".join(short) + "\n\nSave anyway?"):
            return
        self._do_save(db.SALES_FILE, header, add_stock=False, is_sale=True,
                      label="Sale")

    def save_purchase(self):
        header = self._gather()
        if header is None:
            return
        self._do_save(db.PURCHASES_FILE, header, add_stock=True, is_sale=False,
                      label="Purchase")

    def _do_save(self, path, header, add_stock, is_sale, label):
        if self._saving:
            return
        self._set_saving(True)
        try:
            committed = self._commit(path, header, add_stock, is_sale)
        except db.FileLockedError as e:
            self._fail(f"Could not save — {os.path.basename(e.path)} is open in "
                       "Excel/LibreOffice. Close it and try again. "
                       "Nothing was saved.")
            return
        except Exception as e:  # noqa: BLE001
            self._fail(f"Save failed: {e}")
            return
        finally:
            self._set_saving(False)
        if committed:
            self._ok(f"{label} saved: {len(self.lines)} product(s), "
                     f"total {self.total.get()}.")

    def _commit(self, path, header, add_stock, is_sale):
        try:
            duplicate = db.bill_exists(path, header["bill"])
        except Exception:
            duplicate = False
        if duplicate and not messagebox.askyesno(
                "Duplicate bill", f"Bill No {header['bill']} already exists in "
                "this ledger.\n\nSave it again anyway?"):
            return False
        # Abort before writing anything if any target file is locked.
        db.assert_writable(path, db.STOCK_FILE, db.PARTY_FILE)
        subtotal = round(sum(ln["amount"] for ln in self.lines), 2)
        ecs = db.num(self.ecs.get())
        vat_pct = db.num(self.vat_pct.get())
        vat_amount = round(subtotal * vat_pct / 100.0, 2)
        total = round(subtotal + ecs + vat_amount, 2)
        db.append_bill(path, header, self.lines, subtotal, ecs, vat_pct,
                       vat_amount, total)
        for ln in self.lines:
            db.update_stock(ln["product"], ln["qty"], add=add_stock)
        db.update_party(header["pan"], header["vendor"], header["address"],
                        total, is_sale=is_sale)
        return True

    def _set_saving(self, on):
        self._saving = on
        state = "disabled" if on else "normal"
        self.btn_sale.configure(state=state)
        self.btn_purchase.configure(state=state)
        self.update_idletasks()

    def _ok(self, msg):
        self.status.configure(text=msg, fg=GREEN)
        self.clear_form()

    def _fail(self, msg):
        self.status.configure(text=msg, fg=RED)
        messagebox.showerror("Not saved", msg)

    def clear_form(self):
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
        self._refresh_products()
        self._refresh_parties_picker()

    # ------------------------------------------------------------------ #
    # SALES / PURCHASES — grouped by bill
    # ------------------------------------------------------------------ #
    def _build_txn(self, key, title, path):
        page = self._page(key, title,
                          "Grouped by bill — each bill shows its products and total.")
        body = self._card(page, fill="both", expand=True)
        wrap = tk.Frame(body, bg=CARD_BG)
        wrap.pack(fill="both", expand=True)
        headers = db.TXN_HEADERS
        tree = ttk.Treeview(wrap, columns=headers, show="tree headings")
        tree.heading("#0", text="Bill")
        tree.column("#0", width=110, anchor="w", stretch=False)
        for h in headers:
            tree.heading(h, text=h)
            tree.column(h, width=92, anchor="e" if h in RIGHT_COLS else "w",
                        stretch=False)
        tree.tag_configure("bill", background=GROUP_BG,
                           font=(FAMILY, 11, "bold"))
        ysb = ttk.Scrollbar(wrap, orient="vertical", command=tree.yview)
        xsb = ttk.Scrollbar(wrap, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)
        tree.grid(row=0, column=0, sticky="nsew")
        ysb.grid(row=0, column=1, sticky="ns")
        xsb.grid(row=1, column=0, sticky="ew")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)

        grand = tk.StringVar(value="")

        def refresh():
            tree.delete(*tree.get_children())
            rows = self._safe_rows(path, headers)
            i, total = 0, 0.0
            while i < len(rows):
                base = rows[i][0:5]                  # date,bill,pan,vendor,addr
                group = [rows[i]]
                j = i + 1
                while j < len(rows) and rows[j][0:5] == base:
                    group.append(rows[j])
                    j += 1
                f = group[0]
                head_vals = [f[0], f[1], f[2], f[3], f[4], "", "", "", "",
                             f[9], f[10], f[11], f[12], f[13]]
                billtxt = str(f[1]) if f[1] not in (None, "") else "—"
                pid = tree.insert("", "end", text=billtxt, open=True,
                                  tags=("bill",),
                                  values=[self._cell(v) for v in head_vals])
                for r in group:
                    line_vals = ["", "", "", "", "", r[5], r[6], r[7], r[8],
                                 "", "", "", "", ""]
                    tree.insert(pid, "end", text="",
                                values=[self._cell(v) for v in line_vals])
                total += db.num(f[13])
                i = j
            grand.set(f"Grand total:  {self._fmt(total)}")
        self.refreshers[key] = refresh

        bar = tk.Frame(page, bg=CONTENT_BG)
        bar.pack(fill="x", padx=28, pady=(2, 16))
        ttk.Button(bar, text="Open in Excel", style="Primary.TButton",
                   command=lambda: self._open_excel(path, headers)).pack(side="left",
                                                                         padx=(0, 10))
        ttk.Button(bar, text="Refresh", style="Ghost.TButton",
                   command=refresh).pack(side="left")
        tk.Label(bar, textvariable=grand, bg=CONTENT_BG, fg=TEXT,
                 font=F_BOLD).pack(side="right")
        return page

    # ------------------------------------------------------------------ #
    # STOCK page
    # ------------------------------------------------------------------ #
    def _build_stock(self):
        page = self._page("stock", "Stock", "Current quantity on hand.")
        body = self._card(page, fill="both", expand=True)
        tree = self._make_table(body, db.STOCK_HEADERS, width=240,
                                 anchors={"Quantity": "e"})

        def refresh():
            tree.delete(*tree.get_children())
            for r in self._safe_rows(db.STOCK_FILE, db.STOCK_HEADERS):
                tree.insert("", "end", values=[self._cell(v) for v in r])
        self.refreshers["stock"] = refresh

        bar = tk.Frame(page, bg=CONTENT_BG)
        bar.pack(fill="x", padx=28, pady=(2, 16))
        ttk.Button(bar, text="Open in Excel", style="Primary.TButton",
                   command=lambda: self._open_excel(
                       db.STOCK_FILE, db.STOCK_HEADERS)).pack(side="left",
                                                              padx=(0, 10))
        ttk.Button(bar, text="Refresh", style="Ghost.TButton",
                   command=refresh).pack(side="left")
        return page

    # ------------------------------------------------------------------ #
    # PARTIES page
    # ------------------------------------------------------------------ #
    def _build_parties(self):
        page = self._page("parties", "Parties", "Running totals per vendor.")
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

        def render(*_):
            q = query.get().strip().lower()
            tree.delete(*tree.get_children())
            for r in store["rows"]:
                pan, name = str(r[0] or "").lower(), str(r[1] or "").lower()
                if not q or q in pan or q in name:
                    tree.insert("", "end", values=[self._cell(v) for v in r])

        def refresh():
            store["rows"] = self._safe_rows(db.PARTY_FILE, db.PARTY_HEADERS)
            render()
        self.refreshers["parties"] = refresh
        query.trace_add("write", render)

        bar = tk.Frame(page, bg=CONTENT_BG)
        bar.pack(fill="x", padx=28, pady=(2, 16))
        ttk.Button(bar, text="Open in Excel", style="Primary.TButton",
                   command=lambda: self._open_excel(
                       db.PARTY_FILE, db.PARTY_HEADERS)).pack(side="left",
                                                              padx=(0, 10))
        ttk.Button(bar, text="Refresh", style="Ghost.TButton",
                   command=refresh).pack(side="left")
        return page

    # ------------------------------------------------------------------ #
    # shared helpers
    # ------------------------------------------------------------------ #
    def _make_table(self, parent, headers, width=100, anchors=None):
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

    def _safe_rows(self, path, headers):
        try:
            return db.read_rows(path, headers)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("File busy",
                                 f"Could not read {os.path.basename(path)}.\n\n{e}")
            return []

    def _open_excel(self, path, headers):
        ok, err = db.open_file(path, headers)
        if not ok:
            messagebox.showerror("Could not open file",
                                 f"Failed to open:\n{path}\n\n{err}")

    @staticmethod
    def _cell(value):
        if value is None:
            return ""
        if isinstance(value, float) and value == int(value):
            return int(value)
        return value


if __name__ == "__main__":
    InventoryApp().mainloop()
