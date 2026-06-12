"""
Inventory Management — desktop app (Windows / cross-platform).

GUI layer only. All Excel I/O and business rules live in ``storage.py``.

A bill has header details (PAN, Vendor, Address, Bill No, Date) and one or
more product lines (Product Name, Quantity, Amount). VAT defaults to 13% of
the subtotal; Total = Subtotal + ECS + VAT amount (editable).

    SALES     - record the bill as a sale, subtract each product from stock
    PURCHASE  - record the bill as a purchase, add each product to stock
    View Sales / View Purchases / View Stock / View Parties - browse + open Excel
"""

import tkinter as tk
from tkinter import ttk, messagebox
from datetime import date

import storage as db

DEFAULT_VAT = "13"


class InventoryApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Inventory Management")
        self.geometry("780x860")
        self.minsize(720, 800)

        # bill header fields
        self.hdr = {k: tk.StringVar() for k in
                    ("pan", "vendor", "address", "bill", "date")}
        self.hdr["date"].set(date.today().strftime("%d/%m/%Y"))

        # product-line entry fields
        self.line = {k: tk.StringVar() for k in ("product", "qty", "amount")}

        # totals
        self.subtotal = tk.StringVar(value="0")
        self.ecs = tk.StringVar(value="0")
        self.vat_pct = tk.StringVar(value=DEFAULT_VAT)
        self.vat_amt = tk.StringVar(value="0")
        self.total = tk.StringVar(value="0")

        self.lines = []  # list of {product, qty, amount}
        self._build_ui()

    # -- layout ----------------------------------------------------------- #
    def _build_ui(self):
        pad = {"padx": 6, "pady": 4}

        ttk.Label(self, text="Inventory Management",
                  font=("Segoe UI", 16, "bold")).pack(pady=(12, 2))
        ttk.Label(self, text=f"Data saved in: {db.DATA_DIR}",
                  foreground="#666").pack()

        # --- bill header ------------------------------------------------- #
        hf = ttk.LabelFrame(self, text="Bill Details")
        hf.pack(fill="x", padx=16, pady=8)
        header_fields = [
            ("pan", "PAN No"), ("vendor", "Vendor Name"),
            ("address", "Vendor Address"), ("bill", "Bill No"),
            ("date", "Date"),
        ]
        for i, (key, label) in enumerate(header_fields):
            ttk.Label(hf, text=label + ":").grid(
                row=i, column=0, sticky="e", **pad)
            ttk.Entry(hf, textvariable=self.hdr[key], width=44).grid(
                row=i, column=1, sticky="w", **pad)

        # --- products ---------------------------------------------------- #
        pf = ttk.LabelFrame(self, text="Products (add one or more)")
        pf.pack(fill="both", expand=True, padx=16, pady=8)

        entry = ttk.Frame(pf)
        entry.pack(fill="x", pady=4)
        ttk.Label(entry, text="Product Name:").grid(row=0, column=0, **pad)
        ttk.Entry(entry, textvariable=self.line["product"], width=24).grid(
            row=0, column=1, **pad)
        ttk.Label(entry, text="Quantity:").grid(row=0, column=2, **pad)
        ttk.Entry(entry, textvariable=self.line["qty"], width=8).grid(
            row=0, column=3, **pad)
        ttk.Label(entry, text="Amount:").grid(row=0, column=4, **pad)
        ttk.Entry(entry, textvariable=self.line["amount"], width=10).grid(
            row=0, column=5, **pad)
        ttk.Button(entry, text="+ Add Product",
                   command=self.add_product).grid(row=0, column=6, padx=8)

        cols = ("Product Name", "Quantity", "Amount")
        self.items = ttk.Treeview(pf, columns=cols, show="headings", height=6)
        for c, w in zip(cols, (320, 120, 140)):
            self.items.heading(c, text=c)
            self.items.column(c, width=w, anchor="w")
        self.items.pack(fill="both", expand=True, padx=4, pady=4)
        ttk.Button(pf, text="Remove Selected",
                   command=self.remove_selected).pack(pady=2)

        # --- totals ------------------------------------------------------ #
        tf = ttk.LabelFrame(self, text="Totals")
        tf.pack(fill="x", padx=16, pady=8)
        ttk.Label(tf, text="Subtotal (Amount):").grid(
            row=0, column=0, sticky="e", **pad)
        ttk.Entry(tf, textvariable=self.subtotal, width=14,
                  state="readonly").grid(row=0, column=1, sticky="w", **pad)
        ttk.Label(tf, text="ECS:").grid(row=0, column=2, sticky="e", **pad)
        ttk.Entry(tf, textvariable=self.ecs, width=12).grid(
            row=0, column=3, sticky="w", **pad)
        ttk.Label(tf, text="VAT %:").grid(row=1, column=0, sticky="e", **pad)
        ttk.Entry(tf, textvariable=self.vat_pct, width=14).grid(
            row=1, column=1, sticky="w", **pad)
        ttk.Label(tf, text="VAT Amount:").grid(row=1, column=2, sticky="e", **pad)
        ttk.Entry(tf, textvariable=self.vat_amt, width=12,
                  state="readonly").grid(row=1, column=3, sticky="w", **pad)
        ttk.Label(tf, text="Total:", font=("Segoe UI", 10, "bold")).grid(
            row=2, column=0, sticky="e", **pad)
        ttk.Entry(tf, textvariable=self.total, width=14).grid(
            row=2, column=1, sticky="w", **pad)
        ttk.Button(tf, text="Calculate Total",
                   command=self.calculate_total).grid(
            row=2, column=2, columnspan=2, padx=8, sticky="w")

        # --- action buttons --------------------------------------------- #
        ab = ttk.Frame(self)
        ab.pack(pady=8)
        ttk.Button(ab, text="SAVE AS SALE", width=18,
                   command=self.save_sale).grid(row=0, column=0, padx=8)
        ttk.Button(ab, text="SAVE AS PURCHASE", width=18,
                   command=self.save_purchase).grid(row=0, column=1, padx=8)
        ttk.Button(ab, text="Clear Bill", command=self.clear_form).grid(
            row=0, column=2, padx=8)

        # --- view buttons ----------------------------------------------- #
        vb = ttk.LabelFrame(self, text="View / Export")
        vb.pack(fill="x", padx=16, pady=6)
        ttk.Button(vb, text="View Sales",
                   command=lambda: self.show_transactions(
                       "Sales", db.SALES_FILE)).grid(row=0, column=0, padx=6, pady=6)
        ttk.Button(vb, text="View Purchases",
                   command=lambda: self.show_transactions(
                       "Purchases", db.PURCHASES_FILE)).grid(
            row=0, column=1, padx=6, pady=6)
        ttk.Button(vb, text="View Stock",
                   command=self.show_stock).grid(row=0, column=2, padx=6, pady=6)
        ttk.Button(vb, text="View Parties",
                   command=self.show_parties).grid(row=0, column=3, padx=6, pady=6)

        self.status = ttk.Label(self, text="Ready.", foreground="#0a7",
                                font=("Segoe UI", 10))
        self.status.pack(side="bottom", pady=8)

    # -- line items ------------------------------------------------------- #
    def add_product(self):
        product = self.line["product"].get().strip()
        qty = db.num(self.line["qty"].get())
        amount = db.num(self.line["amount"].get())
        if not product:
            messagebox.showerror("Missing data", "Enter a Product Name.")
            return
        if qty <= 0:
            messagebox.showerror("Missing data", "Quantity must be > 0.")
            return
        self.lines.append({"product": product, "qty": qty, "amount": amount})
        self.items.insert("", "end",
                          values=(product, self._fmt(qty), self._fmt(amount)))
        for k in self.line:
            self.line[k].set("")
        self.calculate_total()

    def remove_selected(self):
        sel = self.items.selection()
        if not sel:
            return
        for item in sel:
            idx = self.items.index(item)
            self.items.delete(item)
            del self.lines[idx]
        self.calculate_total()

    # -- totals ----------------------------------------------------------- #
    def calculate_total(self):
        subtotal = sum(ln["amount"] for ln in self.lines)
        vat_amount = subtotal * db.num(self.vat_pct.get()) / 100.0
        total = subtotal + db.num(self.ecs.get()) + vat_amount
        self.subtotal.set(self._fmt(subtotal))
        self.vat_amt.set(self._fmt(vat_amount))
        self.total.set(self._fmt(total))

    @staticmethod
    def _fmt(value: float) -> str:
        value = float(value)
        if value == int(value):
            return str(int(value))
        return f"{value:.2f}"

    # -- save ------------------------------------------------------------- #
    def _gather(self):
        """Collect bill + lines, auto-adding a lone typed product. None on error."""
        if not self.lines and self.line["product"].get().strip():
            self.add_product()
        if not self.lines:
            messagebox.showerror("No products",
                                 "Add at least one product to the bill.")
            return None
        # make sure totals reflect current ecs / vat
        self.calculate_total()
        header = {
            "date": self.hdr["date"].get().strip(),
            "bill": self.hdr["bill"].get().strip(),
            "pan": self.hdr["pan"].get().strip(),
            "vendor": self.hdr["vendor"].get().strip(),
            "address": self.hdr["address"].get().strip(),
        }
        return header

    def save_sale(self):
        header = self._gather()
        if header is None:
            return
        # check stock for every line
        short = []
        for ln in self.lines:
            have = db.stock_on_hand(ln["product"])
            if ln["qty"] > have:
                short.append(f"  {ln['product']}: have {self._fmt(have)}, "
                             f"selling {self._fmt(ln['qty'])}")
        if short:
            if not messagebox.askyesno(
                    "Low stock",
                    "These products will go negative:\n\n"
                    + "\n".join(short) + "\n\nSave anyway?"):
                return
        db.append_bill(db.SALES_FILE, header, self.lines,
                       db.num(self.ecs.get()), db.num(self.vat_pct.get()),
                       db.num(self.vat_amt.get()), db.num(self.total.get()))
        for ln in self.lines:
            db.update_stock(ln["product"], ln["qty"], add=False)
        db.update_party(header["pan"], header["vendor"], header["address"],
                        db.num(self.total.get()), is_sale=True)
        self._ok(f"Sale saved: {len(self.lines)} product(s), "
                 f"total {self.total.get()}.")

    def save_purchase(self):
        header = self._gather()
        if header is None:
            return
        db.append_bill(db.PURCHASES_FILE, header, self.lines,
                       db.num(self.ecs.get()), db.num(self.vat_pct.get()),
                       db.num(self.vat_amt.get()), db.num(self.total.get()))
        for ln in self.lines:
            db.update_stock(ln["product"], ln["qty"], add=True)
        db.update_party(header["pan"], header["vendor"], header["address"],
                        db.num(self.total.get()), is_sale=False)
        self._ok(f"Purchase saved: {len(self.lines)} product(s), "
                 f"total {self.total.get()}.")

    def _ok(self, msg):
        self.status.configure(text=msg, foreground="#0a7")
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

    # -- viewers ---------------------------------------------------------- #
    def show_transactions(self, title, path):
        win = tk.Toplevel(self)
        win.title(title)
        win.geometry("1040x520")
        ttk.Label(win, text=title, font=("Segoe UI", 13, "bold")).pack(pady=6)

        wrap = ttk.Frame(win)
        wrap.pack(fill="both", expand=True, padx=8, pady=4)
        tree = ttk.Treeview(wrap, columns=db.TXN_HEADERS, show="headings")
        for h in db.TXN_HEADERS:
            tree.heading(h, text=h)
            tree.column(h, width=95, anchor="w", stretch=False)
        ysb = ttk.Scrollbar(wrap, orient="vertical", command=tree.yview)
        xsb = ttk.Scrollbar(wrap, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)
        tree.grid(row=0, column=0, sticky="nsew")
        ysb.grid(row=0, column=1, sticky="ns")
        xsb.grid(row=1, column=0, sticky="ew")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)

        for row in db.read_rows(path, db.TXN_HEADERS):
            tree.insert("", "end", values=[self._cell(v) for v in row])

        ttk.Button(win, text="Open Excel File",
                   command=lambda: db.open_file(path, db.TXN_HEADERS)).pack(pady=6)

    def show_stock(self):
        win = tk.Toplevel(self)
        win.title("Current Stock")
        win.geometry("440x480")
        ttk.Label(win, text="Current Stock",
                  font=("Segoe UI", 13, "bold")).pack(pady=6)
        tree = ttk.Treeview(win, columns=db.STOCK_HEADERS, show="headings")
        for h in db.STOCK_HEADERS:
            tree.heading(h, text=h)
            tree.column(h, width=190, anchor="w")
        for row in db.read_rows(db.STOCK_FILE, db.STOCK_HEADERS):
            tree.insert("", "end", values=[self._cell(v) for v in row])
        tree.pack(fill="both", expand=True, padx=10, pady=8)
        ttk.Button(win, text="Open Excel File",
                   command=lambda: db.open_file(
                       db.STOCK_FILE, db.STOCK_HEADERS)).pack(pady=6)

    def show_parties(self):
        win = tk.Toplevel(self)
        win.title("Parties")
        win.geometry("800x520")
        ttk.Label(win, text="Parties",
                  font=("Segoe UI", 13, "bold")).pack(pady=6)

        bar = ttk.Frame(win)
        bar.pack(fill="x", padx=10)
        ttk.Label(bar, text="Search (PAN or Name):").pack(side="left")
        query = tk.StringVar()
        ttk.Entry(bar, textvariable=query, width=30).pack(side="left", padx=6)

        tree = ttk.Treeview(win, columns=db.PARTY_HEADERS, show="headings")
        for h, w in zip(db.PARTY_HEADERS, (110, 150, 200, 95, 105, 105)):
            tree.heading(h, text=h)
            tree.column(h, width=w, anchor="w")
        tree.pack(fill="both", expand=True, padx=10, pady=8)

        all_rows = db.read_rows(db.PARTY_FILE, db.PARTY_HEADERS)

        def refresh(*_):
            q = query.get().strip().lower()
            tree.delete(*tree.get_children())
            for row in all_rows:
                pan = str(row[0] or "").lower()
                name = str(row[1] or "").lower()
                if not q or q in pan or q in name:
                    tree.insert("", "end", values=[self._cell(v) for v in row])

        query.trace_add("write", refresh)
        refresh()
        ttk.Button(win, text="Open Excel File",
                   command=lambda: db.open_file(
                       db.PARTY_FILE, db.PARTY_HEADERS)).pack(pady=6)

    @staticmethod
    def _cell(value):
        if value is None:
            return ""
        if isinstance(value, float) and value == int(value):
            return int(value)
        return value


if __name__ == "__main__":
    InventoryApp().mainloop()
