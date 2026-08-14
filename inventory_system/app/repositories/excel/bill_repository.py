"""Excel-backed BillRepository — thin adapter over the existing storage.py."""
from datetime import datetime
from decimal import Decimal

import storage as db
from app.schemas.bill import BillCreate, BillLineIn, BillOut


class ExcelBillRepository:
    def __init__(self, path: str):
        self._path = path

    def append(self, bill: BillCreate, subtotal: Decimal, vat_amount: Decimal,
              total: Decimal) -> None:
        header = {"date": bill.date.strftime("%d/%m/%Y"), "bill": bill.bill_no,
                  "pan": bill.pan, "vendor": bill.vendor, "address": bill.address}
        lines = [{"product": ln.product, "qty": float(ln.qty),
                  "rate": float(ln.rate), "amount": float(ln.qty * ln.rate)}
                 for ln in bill.lines]
        db.append_bill(self._path, header, lines, float(subtotal), float(bill.ecs),
                       float(bill.vat_percent), float(vat_amount), float(total))

    def exists(self, bill_no: str) -> bool:
        return db.bill_exists(self._path, bill_no)

    def list_all(self) -> list[BillOut]:
        """Groups flat rows back into one BillOut per bill, mirroring the
        legacy inventory_app._build_txn's refresh() grouping logic
        (contiguous rows sharing date/bill/pan/vendor/address = one bill).
        Dates are parsed with the same "%d/%m/%Y" format both this
        repository's append() and the legacy app always write — a
        ValueError here means the underlying data is genuinely malformed,
        not something to silently paper over.
        """
        rows = db.read_rows(self._path, db.TXN_HEADERS)
        bills: list[BillOut] = []
        i = 0
        while i < len(rows):
            base = rows[i][0:5]
            group = [rows[i]]
            j = i + 1
            while j < len(rows) and rows[j][0:5] == base:
                group.append(rows[j])
                j += 1
            head = group[0]
            lines = [BillLineIn(product=str(r[5] or ""), qty=Decimal(str(r[6])),
                                rate=Decimal(str(r[7])))
                    for r in group]
            bills.append(BillOut(
                date=datetime.strptime(str(head[0]), "%d/%m/%Y").date(),
                bill_no=str(head[1] or ""), pan=str(head[2] or ""),
                vendor=str(head[3] or ""), address=str(head[4] or ""), lines=lines,
                subtotal=Decimal(str(head[9])), ecs=Decimal(str(head[10])),
                vat_percent=Decimal(str(head[11])), vat_amount=Decimal(str(head[12])),
                total=Decimal(str(head[13]))))
            i = j
        return bills
