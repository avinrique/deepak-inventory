"""
Inventory Management — desktop app (Windows / cross-platform).

GUI layer only. All Excel I/O and business rules live in ``storage.py``.

Layout: a left sidebar navigates between sections that share one window:
    New Bill   - create a sale/purchase bill with one or more products
    Sales      - browse every sale, open the Excel file
    Purchases  - browse every purchase, open the Excel file
    Stock      - current quantity on hand per product
    Parties    - per-party Sales / Purchases / Combined totals (searchable)

A bill has header details (PAN, Vendor, Address, Bill No, Date) and one or
more product lines. Each line: Product, Quantity, Rate -> Amount = Qty x Rate.
VAT defaults to 13% of the subtotal; Total = Subtotal + ECS + VAT Amount.
Bill-level figures repeat on every product row in the Excel export.
"""

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
F_LABEL = (FAMILY, 11, "bold")
F_HEADING = (FAMILY, 22, "bold")
F_NAV = (FAMILY, 13)
F_BRAND = (FAMILY, 17, "bold")

SIDEBAR_BG = "#111827"
SIDEBAR_FG = "#cbd5e1"
NAV_HOVER = "#1f2937"
NAV_ACTIVE = "#2563eb"
CONTENT_BG = "#eef1f5"
CARD_BG = "#ffffff"
ACCENT = "#2563eb"
ACCENT_DK = "#1d4ed8"
GREEN = "#059669"
GREEN_DK = "#047857"
TEXT = "#111827"
MUTED = "#6b7280"
BORDER = "#e2e6ec"

DEFAULT_VAT = "13"


class InventoryApp(tk.Tk):
    NAV = [("new", "New Bill"), ("sales", "Sales"),
           ("purchases", "Purchases"), ("stock", "Stock"),
           ("parties", "Parties")]

    def __init__(self):
        super().__init__()
        self.title("Inventory Management")
        self.geometry("1180x840")
        self.minsize(1040, 760)
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
        self.lines = []          # list of {product, qty, rate, amount}

        self.frames = {}
        self.nav_buttons = {}
        self.refreshers = {}

        self._build_layout()
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
            st.map(name, background=[("active", active)],
                   foreground=[("active", fg)])

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
                 font=(FAMILY, 10, "bold")).pack(anchor="w", padx=22,
                                                 pady=(0, 22))
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
        elif key in self.refreshers:
            self.refreshers[key]()

    # -- reusable building blocks --------------------------------------- #
    def _page(self, key, title, subtitle=""):
        frame = tk.Frame(self.container, bg=CONTENT_BG)
        bar = tk.Frame(frame, bg=CONTENT_BG)
        bar.pack(fill="x", padx=28, pady=(24, 6))
        tk.Label(bar, text=title, bg=CONTENT_BG, fg=TEXT,
                 font=F_HEADING).pack(side="left")
        if subtitle:
            tk.Label(bar, text=subtitle, bg=CONTENT_BG, fg=MUTED,
                     font=F_SMALL).pack(side="left", padx=12, pady=(14, 0))
        return frame

    def _card(self, parent, title=None, padx=28, fill="x", expand=False):
        outer = tk.Frame(parent, bg=CONTENT_BG)
        outer.pack(fill=fill, expand=expand, padx=padx, pady=8)
        if title:
            tk.Label(outer, text=title.upper(), bg=CONTENT_BG, fg=MUTED,
                     font=(FAMILY, 10, "bold")).pack(anchor="w", pady=(0, 5))
        shell = tk.Frame(outer, bg=CARD_BG, highlightbackground=BORDER,
                         highlightthickness=1, bd=0)
        shell.pack(fill="both", expand=True)
        inner = tk.Frame(shell, bg=CARD_BG)
        inner.pack(fill="both", expand=True, padx=18, pady=16)
        return inner

    @staticmethod
    def _field_label(parent, text, r, c):
        tk.Label(parent, text=text, bg=CARD_BG, fg=TEXT, font=F_BASE,
                 anchor="e").grid(row=r, column=c, sticky="e", padx=(6, 8),
                                  pady=7)

    # ------------------------------------------------------------------ #
    # NEW BILL page
    # ------------------------------------------------------------------ #
    def _build_new(self):
        page = self._page("new", "New Bill",
                          "Fill the details, add products, then save.")

        # --- bill details (2-column) ---
        bd = self._card(page, "Bill Details")
        self._field_label(bd, "PAN No:", 0, 0)
        ttk.Entry(bd, textvariable=self.hdr["pan"], width=26).grid(
            row=0, column=1, sticky="w", pady=7)
        self._field_label(bd, "Bill No:", 0, 2)
        ttk.Entry(bd, textvariable=self.hdr["bill"], width=26).grid(
            row=0, column=3, sticky="w", pady=7)
        self._field_label(bd, "Vendor Name:", 1, 0)
        ttk.Entry(bd, textvariable=self.hdr["vendor"], width=26).grid(
            row=1, column=1, sticky="w", pady=7)
        self._field_label(bd, "Date:", 1, 2)
        ttk.Entry(bd, textvariable=self.hdr["date"], width=26).grid(
            row=1, column=3, sticky="w", pady=7)
        self._field_label(bd, "Vendor Address:", 2, 0)
        ttk.Entry(bd, textvariable=self.hdr["address"], width=62).grid(
            row=2, column=1, columnspan=3, sticky="w", pady=7)

        # --- products ---
        pr = self._card(page, "Products", fill="both", expand=True)
        row = tk.Frame(pr, bg=CARD_BG)
        row.pack(fill="x")
        tk.Label(row, text="Product Name:", bg=CARD_BG, fg=TEXT,
                 font=F_BASE).pack(side="left", padx=(0, 6))
        self.product_cb = ttk.Combobox(row, textvariable=self.line["product"],
                                       width=24, values=db.product_names())
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
        for widget in (self.product_cb, qe, re):
            widget.bind("<Return>", lambda e: self.add_product())

        cols = ("Product Name", "Quantity", "Rate", "Amount")
        tbl = tk.Frame(pr, bg=CARD_BG)
        tbl.pack(fill="both", expand=True, pady=(12, 6))
        self.items = ttk.Treeview(tbl, columns=cols, show="headings", height=6)
        for c, w, anchor in (("Product Name", 300, "w"), ("Quantity", 110, "e"),
                             ("Rate", 110, "e"), ("Amount", 140, "e")):
            self.items.heading(c, text=c)
            self.items.column(c, width=w, anchor=anchor, stretch=(c == "Product Name"))
        ysb = ttk.Scrollbar(tbl, orient="vertical", command=self.items.yview)
        self.items.configure(yscrollcommand=ysb.set)
        self.items.pack(side="left", fill="both", expand=True)
        ysb.pack(side="right", fill="y")
        ttk.Button(pr, text="Remove Selected", style="Ghost.TButton",
                   command=self.remove_selected).pack(anchor="w")

        # --- totals ---
        tt = self._card(page, "Totals")
        self._field_label(tt, "Subtotal:", 0, 0)
        ttk.Entry(tt, textvariable=self.subtotal, width=16,
                  state="readonly").grid(row=0, column=1, sticky="w", pady=7)
        self._field_label(tt, "ECS:", 0, 2)
        ee = ttk.Entry(tt, textvariable=self.ecs, width=16)
        ee.grid(row=0, column=3, sticky="w", pady=7)
        self._field_label(tt, "VAT %:", 1, 0)
        ve = ttk.Entry(tt, textvariable=self.vat_pct, width=16)
        ve.grid(row=1, column=1, sticky="w", pady=7)
        self._field_label(tt, "VAT Amount:", 1, 2)
        ttk.Entry(tt, textvariable=self.vat_amt, width=16,
                  state="readonly").grid(row=1, column=3, sticky="w", pady=7)
        tk.Label(tt, text="Total:", bg=CARD_BG, fg=TEXT, font=F_BOLD,
                 anchor="e").grid(row=2, column=0, sticky="e", padx=(6, 8),
                                  pady=7)
        ttk.Entry(tt, textvariable=self.total, width=16,
                  font=F_BOLD).grid(row=2, column=1, sticky="w", pady=7)
        ttk.Button(tt, text="Calculate Total", style="Ghost.TButton",
                   command=self.calculate_total).grid(
            row=2, column=2, columnspan=2, sticky="w", padx=8)
        for widget in (ee, ve):
            widget.bind("<KeyRelease>", lambda e: self.calculate_total())

        # --- actions ---
        act = tk.Frame(page, bg=CONTENT_BG)
        act.pack(fill="x", padx=28, pady=(6, 4))
        ttk.Button(act, text="SAVE AS SALE", style="Success.TButton",
                   command=self.save_sale).pack(side="left", padx=(0, 10))
        ttk.Button(act, text="SAVE AS PURCHASE", style="Primary.TButton",
                   command=self.save_purchase).pack(side="left", padx=(0, 10))
        ttk.Button(act, text="Clear Bill", style="Ghost.TButton",
                   command=self.clear_form).pack(side="left")

        self.status = tk.Label(page, text="Ready.", bg=CONTENT_BG, fg=GREEN,
                               font=F_BASE)
        self.status.pack(anchor="w", padx=28, pady=(2, 12))
        return page

    def _refresh_products(self):
        if hasattr(self, "product_cb"):
            self.product_cb["values"] = db.product_names()

    # -- line items ----------------------------------------------------- #
    def add_product(self):
        product = self.line["product"].get().strip()
        qty = db.num(self.line["qty"].get())
        rate = db.num(self.line["rate"].get())
        if not product:
            messagebox.showerror("Missing data", "Enter a Product Name.")
            return
        if qty <= 0:
            messagebox.showerror("Missing data", "Quantity must be > 0.")
            return
        amount = qty * rate
        self.lines.append({"product": product, "qty": qty,
                           "rate": rate, "amount": amount})
        self.items.insert("", "end", values=(
            product, self._fmt(qty), self._fmt(rate), self._fmt(amount)))
        for k in self.line:
            self.line[k].set("")
        self.calculate_total()
        self.product_cb.focus_set()

    def remove_selected(self):
        for item in self.items.selection():
            idx = self.items.index(item)
            self.items.delete(item)
            del self.lines[idx]
        self.calculate_total()

    # -- totals --------------------------------------------------------- #
    def calculate_total(self):
        subtotal = sum(ln["amount"] for ln in self.lines)
        vat_amount = subtotal * db.num(self.vat_pct.get()) / 100.0
        total = subtotal + db.num(self.ecs.get()) + vat_amount
        self.subtotal.set(self._fmt(subtotal))
        self.vat_amt.set(self._fmt(vat_amount))
        self.total.set(self._fmt(total))

    @staticmethod
    def _fmt(value):
        value = float(value)
        if value == int(value):
            return str(int(value))
        return f"{value:.2f}"

    # -- save ----------------------------------------------------------- #
    def _gather(self):
        if not self.lines and self.line["product"].get().strip():
            self.add_product()
        if not self.lines:
            messagebox.showerror("No products",
                                 "Add at least one product to the bill.")
            return None
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
        self._commit(db.SALES_FILE, header, add_stock=False, is_sale=True)
        self._ok(f"Sale saved: {len(self.lines)} product(s), "
                 f"total {self.total.get()}.")

    def save_purchase(self):
        header = self._gather()
        if header is None:
            return
        self._commit(db.PURCHASES_FILE, header, add_stock=True, is_sale=False)
        self._ok(f"Purchase saved: {len(self.lines)} product(s), "
                 f"total {self.total.get()}.")

    def _commit(self, path, header, add_stock, is_sale):
        db.append_bill(path, header, self.lines, db.num(self.ecs.get()),
                       db.num(self.vat_pct.get()), db.num(self.vat_amt.get()),
                       db.num(self.total.get()))
        for ln in self.lines:
            db.update_stock(ln["product"], ln["qty"], add=add_stock)
        db.update_party(header["pan"], header["vendor"], header["address"],
                        db.num(self.total.get()), is_sale=is_sale)

    def _ok(self, msg):
        self.status.configure(text=msg, fg=GREEN)
        self.clear_form()

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

    # ------------------------------------------------------------------ #
    # TRANSACTION pages (sales / purchases)
    # ------------------------------------------------------------------ #
    def _build_txn(self, key, title, path):
        page = self._page(key, title, "Every product line recorded.")
        body = self._card(page, fill="both", expand=True)
        tree = self._make_table(body, db.TXN_HEADERS, width=92)

        def refresh():
            tree.delete(*tree.get_children())
            for r in db.read_rows(path, db.TXN_HEADERS):
                tree.insert("", "end", values=[self._cell(v) for v in r])
        self.refreshers[key] = refresh

        bar = tk.Frame(page, bg=CONTENT_BG)
        bar.pack(fill="x", padx=28, pady=(2, 16))
        ttk.Button(bar, text="Open in Excel", style="Primary.TButton",
                   command=lambda: db.open_file(path, db.TXN_HEADERS)).pack(
            side="left", padx=(0, 10))
        ttk.Button(bar, text="Refresh", style="Ghost.TButton",
                   command=refresh).pack(side="left")
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
            for r in db.read_rows(db.STOCK_FILE, db.STOCK_HEADERS):
                tree.insert("", "end", values=[self._cell(v) for v in r])
        self.refreshers["stock"] = refresh

        bar = tk.Frame(page, bg=CONTENT_BG)
        bar.pack(fill="x", padx=28, pady=(2, 16))
        ttk.Button(bar, text="Open in Excel", style="Primary.TButton",
                   command=lambda: db.open_file(
                       db.STOCK_FILE, db.STOCK_HEADERS)).pack(side="left",
                                                              padx=(0, 10))
        ttk.Button(bar, text="Refresh", style="Ghost.TButton",
                   command=refresh).pack(side="left")
        return page

    # ------------------------------------------------------------------ #
    # PARTIES page
    # ------------------------------------------------------------------ #
    def _build_parties(self):
        page = self._page("parties", "Parties",
                          "Running totals per vendor.")
        sb = tk.Frame(page, bg=CONTENT_BG)
        sb.pack(fill="x", padx=28, pady=(0, 2))
        tk.Label(sb, text="Search (PAN or Name):", bg=CONTENT_BG, fg=TEXT,
                 font=F_BASE).pack(side="left", padx=(0, 8))
        query = tk.StringVar()
        ttk.Entry(sb, textvariable=query, width=32).pack(side="left")

        body = self._card(page, fill="both", expand=True)
        anchors = {h: "e" for h in
                   ("Total Sales", "Total Purchases", "Total Combined")}
        tree = self._make_table(body, db.PARTY_HEADERS, width=130,
                                 anchors=anchors)
        store = {"rows": []}

        def render(*_):
            q = query.get().strip().lower()
            tree.delete(*tree.get_children())
            for r in store["rows"]:
                pan = str(r[0] or "").lower()
                name = str(r[1] or "").lower()
                if not q or q in pan or q in name:
                    tree.insert("", "end", values=[self._cell(v) for v in r])

        def refresh():
            store["rows"] = db.read_rows(db.PARTY_FILE, db.PARTY_HEADERS)
            render()
        self.refreshers["parties"] = refresh
        query.trace_add("write", render)

        bar = tk.Frame(page, bg=CONTENT_BG)
        bar.pack(fill="x", padx=28, pady=(2, 16))
        ttk.Button(bar, text="Open in Excel", style="Primary.TButton",
                   command=lambda: db.open_file(
                       db.PARTY_FILE, db.PARTY_HEADERS)).pack(side="left",
                                                              padx=(0, 10))
        ttk.Button(bar, text="Refresh", style="Ghost.TButton",
                   command=refresh).pack(side="left")
        return page

    # ------------------------------------------------------------------ #
    # table helper
    # ------------------------------------------------------------------ #
    def _make_table(self, parent, headers, width=100, anchors=None):
        anchors = anchors or {}
        wrap = tk.Frame(parent, bg=CARD_BG)
        wrap.pack(fill="both", expand=True)
        tree = ttk.Treeview(wrap, columns=headers, show="headings")
        for h in headers:
            tree.heading(h, text=h)
            tree.column(h, width=width, anchor=anchors.get(h, "w"),
                        stretch=False)
        ysb = ttk.Scrollbar(wrap, orient="vertical", command=tree.yview)
        xsb = ttk.Scrollbar(wrap, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)
        tree.grid(row=0, column=0, sticky="nsew")
        ysb.grid(row=0, column=1, sticky="ns")
        xsb.grid(row=1, column=0, sticky="ew")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        return tree

    @staticmethod
    def _cell(value):
        if value is None:
            return ""
        if isinstance(value, float) and value == int(value):
            return int(value)
        return value


if __name__ == "__main__":
    InventoryApp().mainloop()
