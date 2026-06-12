"""
Inventory Management — desktop app (Windows / cross-platform).

GUI layer only. All Excel I/O and business rules live in ``storage.py``.

Buttons:
    SALES     - record a sale, subtract quantity from stock, add to party sales
    PURCHASE  - record a purchase, add quantity to stock, add to party purchases
    PARTY     - browse/search parties (by PAN or name) with running totals
    STOCK     - browse current stock on hand
"""

import tkinter as tk
from tkinter import ttk, messagebox
from datetime import date

import storage as db


class InventoryApp(tk.Tk):
    FIELDS = [
        ("pan", "PAN No"),
        ("vendor", "Vendor Name"),
        ("address", "Vendor Address"),
        ("bill", "Bill No"),
        ("product", "Product Name"),
        ("quantity", "Product Quantity"),
        ("date", "Date"),
        ("amount", "Amount"),
        ("ecs", "ECS"),
        ("vat", "VAT"),
        ("total", "Total"),
    ]

    def __init__(self):
        super().__init__()
        self.title("Inventory Management")
        self.geometry("600x640")
        self.resizable(False, False)
        self.vars = {key: tk.StringVar() for key, _ in self.FIELDS}
        self.vars["date"].set(date.today().strftime("%d/%m/%Y"))
        self._build_ui()

    # -- layout ----------------------------------------------------------- #
    def _build_ui(self):
        pad = {"padx": 8, "pady": 5}

        ttk.Label(self, text="Inventory Management",
                  font=("Segoe UI", 16, "bold")).pack(pady=(14, 4))
        ttk.Label(self, text=f"Data saved in: {db.DATA_DIR}",
                  foreground="#666").pack()

        form = ttk.Frame(self)
        form.pack(fill="x", padx=20, pady=10)

        for i, (key, label) in enumerate(self.FIELDS):
            ttk.Label(form, text=label + ":").grid(
                row=i, column=0, sticky="e", **pad)
            ttk.Entry(form, textvariable=self.vars[key], width=40).grid(
                row=i, column=1, sticky="w", **pad)

        total_row = next(i for i, (k, _) in enumerate(self.FIELDS)
                         if k == "total")
        ttk.Button(form, text="Calculate Total",
                   command=self.calculate_total).grid(
            row=total_row, column=2, padx=6)

        btns = ttk.Frame(self)
        btns.pack(pady=14)
        ttk.Button(btns, text="SALES", width=14,
                   command=self.save_sale).grid(row=0, column=0, padx=6, pady=4)
        ttk.Button(btns, text="PURCHASE", width=14,
                   command=self.save_purchase).grid(row=0, column=1, padx=6, pady=4)
        ttk.Button(btns, text="PARTY", width=14,
                   command=self.show_parties).grid(row=1, column=0, padx=6, pady=4)
        ttk.Button(btns, text="STOCK", width=14,
                   command=self.show_stock).grid(row=1, column=1, padx=6, pady=4)

        ttk.Button(self, text="Clear Form", command=self.clear_form).pack()

        self.status = ttk.Label(self, text="Ready.", foreground="#0a7",
                                font=("Segoe UI", 10))
        self.status.pack(side="bottom", pady=10)

    # -- helpers ---------------------------------------------------------- #
    def calculate_total(self):
        total = (db.num(self.vars["amount"].get())
                 + db.num(self.vars["ecs"].get())
                 + db.num(self.vars["vat"].get()))
        self.vars["total"].set(self._fmt(total))

    @staticmethod
    def _fmt(value: float) -> str:
        if value == int(value):
            return str(int(value))
        return f"{value:.2f}"

    def _collect(self):
        """Validate and return form data as a dict, or None on error."""
        product = self.vars["product"].get().strip()
        if not product:
            messagebox.showerror("Missing data", "Product Name is required.")
            return None
        qty = db.num(self.vars["quantity"].get())
        if qty <= 0:
            messagebox.showerror("Missing data",
                                 "Product Quantity must be greater than 0.")
            return None
        if not self.vars["total"].get().strip():
            self.calculate_total()
        return {
            "date": self.vars["date"].get().strip(),
            "bill": self.vars["bill"].get().strip(),
            "pan": self.vars["pan"].get().strip(),
            "vendor": self.vars["vendor"].get().strip(),
            "address": self.vars["address"].get().strip(),
            "product": product,
            "qty": qty,
            "amount": db.num(self.vars["amount"].get()),
            "ecs": db.num(self.vars["ecs"].get()),
            "vat": db.num(self.vars["vat"].get()),
            "total": db.num(self.vars["total"].get()),
        }

    @staticmethod
    def _row(d):
        return [d["date"], d["bill"], d["pan"], d["vendor"], d["address"],
                d["product"], d["qty"], d["amount"], d["ecs"], d["vat"],
                d["total"]]

    # -- actions ---------------------------------------------------------- #
    def save_sale(self):
        d = self._collect()
        if not d:
            return
        on_hand = db.stock_on_hand(d["product"])
        if d["qty"] > on_hand:
            if not messagebox.askyesno(
                    "Low stock",
                    f"Only {self._fmt(on_hand)} of '{d['product']}' in stock, "
                    f"but selling {self._fmt(d['qty'])}.\n\n"
                    "Save anyway? (stock will go negative)"):
                return
        db.append_transaction(db.SALES_FILE, self._row(d))
        new_qty = db.update_stock(d["product"], d["qty"], add=False)
        db.update_party(d["pan"], d["vendor"], d["address"], d["total"],
                        is_sale=True)
        self._ok(f"Sale saved. '{d['product']}' stock now {self._fmt(new_qty)}.")

    def save_purchase(self):
        d = self._collect()
        if not d:
            return
        db.append_transaction(db.PURCHASES_FILE, self._row(d))
        new_qty = db.update_stock(d["product"], d["qty"], add=True)
        db.update_party(d["pan"], d["vendor"], d["address"], d["total"],
                        is_sale=False)
        self._ok(f"Purchase saved. '{d['product']}' stock now "
                 f"{self._fmt(new_qty)}.")

    def _ok(self, msg):
        self.status.configure(text=msg, foreground="#0a7")
        self.clear_form()

    def clear_form(self):
        for key in self.vars:
            self.vars[key].set("")
        self.vars["date"].set(date.today().strftime("%d/%m/%Y"))

    # -- viewers ---------------------------------------------------------- #
    def show_stock(self):
        win = tk.Toplevel(self)
        win.title("Current Stock")
        win.geometry("420x460")
        ttk.Label(win, text="Current Stock",
                  font=("Segoe UI", 13, "bold")).pack(pady=8)
        tree = ttk.Treeview(win, columns=db.STOCK_HEADERS, show="headings")
        for h in db.STOCK_HEADERS:
            tree.heading(h, text=h)
            tree.column(h, width=180, anchor="w")
        for row in db.read_rows(db.STOCK_FILE, db.STOCK_HEADERS):
            tree.insert("", "end", values=[self._cell(v) for v in row])
        tree.pack(fill="both", expand=True, padx=10, pady=10)

    def show_parties(self):
        win = tk.Toplevel(self)
        win.title("Parties")
        win.geometry("780x500")
        ttk.Label(win, text="Parties",
                  font=("Segoe UI", 13, "bold")).pack(pady=8)

        bar = ttk.Frame(win)
        bar.pack(fill="x", padx=10)
        ttk.Label(bar, text="Search (PAN or Name):").pack(side="left")
        query = tk.StringVar()
        ttk.Entry(bar, textvariable=query, width=30).pack(side="left", padx=6)

        tree = ttk.Treeview(win, columns=db.PARTY_HEADERS, show="headings")
        widths = [110, 150, 200, 90, 100, 100]
        for h, w in zip(db.PARTY_HEADERS, widths):
            tree.heading(h, text=h)
            tree.column(h, width=w, anchor="w")
        tree.pack(fill="both", expand=True, padx=10, pady=10)

        all_rows = db.read_rows(db.PARTY_FILE, db.PARTY_HEADERS)

        def refresh(*_):
            q = query.get().strip().lower()
            tree.delete(*tree.get_children())
            for row in all_rows:
                pan = str(row[0] or "").lower()
                name = str(row[1] or "").lower()
                if not q or q in pan or q in name:
                    tree.insert("", "end",
                                values=[self._cell(v) for v in row])

        query.trace_add("write", refresh)
        refresh()

    @staticmethod
    def _cell(value):
        if value is None:
            return ""
        if isinstance(value, float) and value == int(value):
            return int(value)
        return value


if __name__ == "__main__":
    InventoryApp().mainloop()
