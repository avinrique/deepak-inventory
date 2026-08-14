"""Invoice preview/print/save — the UI surface for the "Generate PDF /
Preview / Print / Save PDF / Re-generate invoice" actions. Data assembly
(SalesService.get_invoice_document) and PDF rendering
(app.reports.sales_invoice_pdf.render_invoice_pdf) both run on a
background Worker — assembly is a DB round trip, rendering is CPU-bound
file I/O, neither touches a Qt GUI object, so unlike Print (which paints
onto a QPrinter, a GUI object) both are safe off the UI thread.

In-app preview uses PySide6.QtPdfWidgets.QPdfView (already part of the
PySide6 install this project requires — no extra dependency) rather than
shelling out to an external viewer, so Preview is a real embedded view,
not "open the file and hope something can display it."
"""
import logging
import os
import shutil
import tempfile
import uuid
from datetime import datetime

from PySide6.QtCore import QSize, Qt, QThreadPool
from PySide6.QtGui import QPainter
from PySide6.QtPdf import QPdfDocument
from PySide6.QtPdfWidgets import QPdfView
from PySide6.QtPrintSupport import QPrintDialog, QPrinter
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from app.reports.sales_invoice_pdf import render_invoice_pdf
from app.services.sales_service import SalesService
from app.ui.theme import MUTED
from app.workers.base_worker import Worker

_logger = logging.getLogger(__name__)


def _print_pdf_document(pdf_document: QPdfDocument, parent) -> bool:
    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    dialog = QPrintDialog(printer, parent)
    if dialog.exec() != QPrintDialog.DialogCode.Accepted:
        return False

    painter = QPainter(printer)
    try:
        target = painter.viewport()
        for page in range(pdf_document.pageCount()):
            if page > 0:
                printer.newPage()
            page_size = pdf_document.pagePointSize(page)
            # Render at 4x the page's point size for print-quality
            # resolution, then scale down to fit the printer's paintable
            # area — QPdfDocument has no direct "print this page" call, so
            # rasterizing and drawing onto the printer's QPainter is the
            # straightforward way to get a PDF onto paper via Qt.
            image = pdf_document.render(page, QSize(int(page_size.width() * 4),
                                                     int(page_size.height() * 4)))
            scaled = image.scaled(target.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                  Qt.TransformationMode.SmoothTransformation)
            x = target.x() + (target.width() - scaled.width()) // 2
            y = target.y() + (target.height() - scaled.height()) // 2
            painter.drawImage(x, y, scaled)
    finally:
        painter.end()
    return True


class InvoicePreviewDialog(QDialog):
    def __init__(self, sales_service: SalesService, invoice_id: uuid.UUID, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Invoice Preview")
        self.resize(760, 920)
        self._sales_service = sales_service
        self._invoice_id = invoice_id
        self._pdf_document = QPdfDocument(self)
        self._current_path: str | None = None

        layout = QVBoxLayout(self)

        toolbar = QHBoxLayout()
        self._status_label = QLabel("")
        self._status_label.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        toolbar.addWidget(self._status_label)
        toolbar.addStretch()

        self._regenerate_button = QPushButton("Regenerate")
        self._save_button = QPushButton("Save PDF As…")
        self._print_button = QPushButton("Print")
        for button, handler in [(self._regenerate_button, self._generate),
                                (self._save_button, self._save_as),
                                (self._print_button, self._print)]:
            button.setObjectName("ghost")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(handler)
            toolbar.addWidget(button)
        layout.addLayout(toolbar)

        self._pdf_view = QPdfView()
        self._pdf_view.setDocument(self._pdf_document)
        self._pdf_view.setPageMode(QPdfView.PageMode.MultiPage)
        self._pdf_view.setZoomMode(QPdfView.ZoomMode.FitToWidth)
        layout.addWidget(self._pdf_view, stretch=1)

        self._generate()

    def _set_busy(self, busy: bool) -> None:
        for button in (self._regenerate_button, self._save_button, self._print_button):
            button.setEnabled(not busy)

    def _generate(self) -> None:
        self._set_busy(True)
        self._status_label.setText("Generating…")
        fd, path = tempfile.mkstemp(suffix=".pdf", prefix="invoice_")
        os.close(fd)

        def work():
            data = self._sales_service.get_invoice_document(self._invoice_id)
            return render_invoice_pdf(data, path)

        worker = Worker(work)
        worker.signals.finished.connect(self._on_generated)
        worker.signals.error.connect(self._on_error)
        QThreadPool.globalInstance().start(worker)

    def _on_generated(self, path: str) -> None:
        self._set_busy(False)
        previous = self._current_path
        self._current_path = path
        self._pdf_document.load(path)
        if previous and previous != path and os.path.exists(previous):
            try:
                os.remove(previous)
            except OSError:
                pass  # best-effort temp-file cleanup, not worth failing over
        self._status_label.setText(f"Generated {datetime.now():%H:%M:%S}")

    def _on_error(self, exc: Exception) -> None:
        self._set_busy(False)
        _logger.exception("Invoice PDF generation failed", exc_info=exc)
        self._status_label.setText("Failed to generate invoice.")
        QMessageBox.critical(self, "Couldn't generate invoice", str(exc))

    def _save_as(self) -> None:
        if self._current_path is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save Invoice PDF", "invoice.pdf",
                                              "PDF Files (*.pdf)")
        if not path:
            return
        try:
            shutil.copyfile(self._current_path, path)
            self._status_label.setText(f"Saved to {path}")
        except OSError as exc:
            _logger.exception("Saving invoice PDF failed", exc_info=exc)
            QMessageBox.critical(self, "Save failed", str(exc))

    def _print(self) -> None:
        if self._current_path is None:
            return
        try:
            if _print_pdf_document(self._pdf_document, self):
                self._status_label.setText("Sent to printer.")
        except Exception as exc:  # noqa: BLE001 - surfaced to the user, not swallowed
            _logger.exception("Printing invoice failed", exc_info=exc)
            QMessageBox.critical(self, "Print failed", str(exc))

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self._current_path and os.path.exists(self._current_path):
            try:
                os.remove(self._current_path)
            except OSError:
                pass
        super().closeEvent(event)
