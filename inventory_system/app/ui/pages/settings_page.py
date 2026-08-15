"""Settings page — a tabbed interface over every Settings section (see
app.models.organization's module docstring for the full field list):

- "Company": name, legal name, tax/VAT id, address, contact details, logo.
- "Inventory": negative-stock policy, default warehouse, low-stock
  behavior, stock valuation method.
- "Sales": invoice number prefix/format, default tax %, default discount %.
- "Purchasing": purchase order number prefix/format.
- "Security": session timeout, password policy.
- "Backup & Restore": backup location/frequency/retention, plus the
  existing create/verify/restore history table.

Every tab except Backup & Restore is a view over the same Organization
snapshot, loaded once; each tab saves only the fields it owns via a
partial OrganizationUpdate. Read-only for anyone without settings.manage
(fields disabled, not hidden, so a viewer can still see current values) —
OrganizationService enforces the same permission independently, so a
client-side bypass of the disabled state still wouldn't let a write
through. Backup & Restore is additionally gated by the stricter
backup.create/backup.restore permissions (already OWNER/ADMIN-only), on
top of settings.manage for the location/frequency/retention fields.
"""
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from PySide6.QtCore import QThreadPool, Qt
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app.domain.backup import BackupFrequency
from app.domain.inventory import LowStockBehavior, StockValuationMethod
from app.domain.purchasing import format_purchase_order_number
from app.domain.sales import format_invoice_number
from app.schemas.backup import BackupOut
from app.schemas.organization import OrganizationOut, OrganizationUpdate
from app.security.session import SessionManager
from app.services.backup_service import BackupService
from app.services.inventory_service import InventoryService
from app.services.organization_service import OrganizationService
from app.ui.theme import MUTED, RED
from app.ui.widgets.async_content import AsyncContentArea
from app.ui.widgets.combo_utils import select_by_data as _select_by_data
from app.ui.widgets.confirm_dialog import confirm_typed
from app.ui.widgets.page_header import PageHeader
from app.ui.widgets.states import EmptyStateWidget
from app.workers.base_worker import Worker

_logger = logging.getLogger(__name__)

_BACKUP_COLUMNS = ["Created", "Filename", "Size", "Status", "Verified"]
_LOGO_PREVIEW_SIZE = 96


def _human_size(num_bytes: int | None) -> str:
    if num_bytes is None:
        return "—"
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def _parse_decimal(text: str, field_name: str) -> Decimal | None:
    try:
        return Decimal(text.strip() or "0")
    except InvalidOperation:
        return None


def _parse_int(text: str) -> int | None:
    try:
        return int(text.strip())
    except ValueError:
        return None


class SettingsPage(QWidget):
    def __init__(self, organization_service: OrganizationService,
                backup_service: BackupService, inventory_service: InventoryService,
                sessions: SessionManager):
        super().__init__()
        self._organization_service = organization_service
        self._backup_service = backup_service
        self._inventory_service = inventory_service
        self._sessions = sessions

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(PageHeader("Settings", "Company profile, operations, security, "
                                                "and backup — all in one place."))

        self._async_area = AsyncContentArea(
            load=self._organization_service.get_current_organization,
            render=self._render_tabs,
            is_empty=lambda org: False,
            empty_state=EmptyStateWidget("No organization", icon="🏢"),
            error_message="Couldn't load settings.")
        layout.addWidget(self._async_area, stretch=1)

    def refresh(self) -> None:
        self._async_area.reload()

    def _can_edit(self) -> bool:
        session = self._sessions.current(now=datetime.now(timezone.utc))
        return session.is_superuser or "settings.manage" in session.permissions

    # -- shared tab scaffold ------------------------------------------- #
    def _build_tab(self, *, rows: list[tuple[str, QWidget]], can_edit: bool,
                   build_update: Callable[[], OrganizationUpdate | None],
                   extra_widgets: list[QWidget] | None = None) -> QWidget:
        container = QWidget()
        outer = QVBoxLayout(container)
        outer.setContentsMargins(28, 20, 28, 24)
        outer.setSpacing(16)

        for widget in extra_widgets or []:
            outer.addWidget(widget)

        card = QWidget()
        card.setObjectName("card")
        card.setMaximumWidth(560)
        form = QFormLayout(card)
        form.setContentsMargins(24, 20, 24, 20)
        form.setSpacing(12)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        for label, widget in rows:
            widget.setEnabled(can_edit)
            form.addRow(label, widget)
        outer.addWidget(card)

        status_label = QLabel("" if can_edit else
                              "You don't have permission to edit these settings.")
        status_label.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        outer.addWidget(status_label)

        if can_edit:
            save_button = QPushButton("Save Changes")
            save_button.setObjectName("primary")
            save_button.setCursor(Qt.CursorShape.PointingHandCursor)
            save_button.setMaximumWidth(160)

            def on_save():
                data = build_update()
                if data is None:
                    return
                save_button.setEnabled(False)
                status_label.setText("Saving…")
                worker = Worker(self._organization_service.update_organization, data)
                worker.signals.finished.connect(
                    lambda _: self._on_saved(status_label, save_button))
                worker.signals.error.connect(
                    lambda exc: self._on_save_error(exc, status_label, save_button))
                QThreadPool.globalInstance().start(worker)

            save_button.clicked.connect(on_save)
            outer.addWidget(save_button)

        outer.addStretch()
        return container

    def _on_saved(self, status_label: QLabel, save_button: QPushButton) -> None:
        save_button.setEnabled(True)
        status_label.setText("Saved.")

    def _on_save_error(self, exc: Exception, status_label: QLabel,
                       save_button: QPushButton) -> None:
        save_button.setEnabled(True)
        _logger.exception("Saving settings failed", exc_info=exc)
        status_label.setText("")
        QMessageBox.critical(self, "Save failed", str(exc))

    # -- tabs ------------------------------------------------------------- #
    def _render_tabs(self, org: OrganizationOut) -> QWidget:
        can_edit = self._can_edit()
        tabs = QTabWidget()
        tabs.addTab(self._build_company_tab(org, can_edit), "Company")
        tabs.addTab(self._build_inventory_tab(org, can_edit), "Inventory")
        tabs.addTab(self._build_sales_tab(org, can_edit), "Sales")
        tabs.addTab(self._build_purchasing_tab(org, can_edit), "Purchasing")
        tabs.addTab(self._build_security_tab(org, can_edit), "Security")
        tabs.addTab(_BackupPanel(self._backup_service, self._organization_service,
                                 self._sessions, org, can_edit),
                   "Backup && Restore")
        return tabs

    # -- Company ------------------------------------------------------------#
    def _build_company_tab(self, org: OrganizationOut, can_edit: bool) -> QWidget:
        fields = {
            "name": QLineEdit(org.name),
            "legal_name": QLineEdit(org.legal_name or ""),
            "tax_id": QLineEdit(org.tax_id or ""),
            "address": QLineEdit(org.address or ""),
            "phone": QLineEdit(org.phone or ""),
            "email": QLineEdit(org.email or ""),
            "website": QLineEdit(org.website or ""),
        }
        labels = {
            "name": "Company Name *", "legal_name": "Legal Name", "tax_id": "Tax / VAT ID",
            "address": "Address", "phone": "Phone", "email": "Email", "website": "Website",
        }

        logo_widget = _LogoEditor(self._organization_service, org, can_edit)

        def build_update() -> OrganizationUpdate | None:
            data = OrganizationUpdate(
                name=fields["name"].text().strip(),
                legal_name=fields["legal_name"].text().strip() or None,
                tax_id=fields["tax_id"].text().strip() or None,
                address=fields["address"].text().strip() or None,
                phone=fields["phone"].text().strip() or None,
                email=fields["email"].text().strip() or None,
                website=fields["website"].text().strip() or None)
            logo_widget.apply_to(data)
            return data

        rows = [(labels[k], w) for k, w in fields.items()]
        return self._build_tab(rows=rows, can_edit=can_edit, build_update=build_update,
                               extra_widgets=[logo_widget])

    # -- Inventory ----------------------------------------------------------#
    def _build_inventory_tab(self, org: OrganizationOut, can_edit: bool) -> QWidget:
        allow_negative = QCheckBox("Allow inventory to go negative")
        allow_negative.setChecked(org.allow_negative_stock)

        warehouse_combo = QComboBox()
        warehouse_combo.addItem("— None —", None)
        _select_by_data(warehouse_combo, org.default_warehouse_id)
        worker = Worker(self._inventory_service.list_warehouses)
        worker.signals.finished.connect(
            lambda whs: self._on_warehouses_loaded(whs, warehouse_combo, org.default_warehouse_id))
        QThreadPool.globalInstance().start(worker)

        low_stock_combo = QComboBox()
        low_stock_combo.addItem("Warn only", LowStockBehavior.WARN_ONLY)
        low_stock_combo.addItem("Block the sale", LowStockBehavior.BLOCK_SALE)
        _select_by_data(low_stock_combo, org.low_stock_behavior)

        valuation_combo = QComboBox()
        for method in StockValuationMethod:
            valuation_combo.addItem(method.value.replace("_", " ").title(), method)
        _select_by_data(valuation_combo, org.stock_valuation_method)

        def build_update() -> OrganizationUpdate:
            return OrganizationUpdate(
                allow_negative_stock=allow_negative.isChecked(),
                default_warehouse_id=warehouse_combo.currentData(),
                low_stock_behavior=low_stock_combo.currentData(),
                stock_valuation_method=valuation_combo.currentData())

        rows = [("", allow_negative), ("Default Warehouse", warehouse_combo),
               ("Low-Stock Behavior", low_stock_combo),
               ("Stock Valuation Method", valuation_combo)]
        return self._build_tab(rows=rows, can_edit=can_edit, build_update=build_update)

    def _on_warehouses_loaded(self, warehouses, combo: QComboBox,
                              selected_id) -> None:
        for wh in warehouses:
            combo.addItem(f"{wh.name} ({wh.code})", wh.id)
        _select_by_data(combo, selected_id)

    # -- Sales ------------------------------------------------------------ #
    def _build_sales_tab(self, org: OrganizationOut, can_edit: bool) -> QWidget:
        prefix_field = QLineEdit(org.invoice_number_prefix)
        prefix_example = QLabel(f"Example: {format_invoice_number(org.invoice_number_prefix, 1)}")
        prefix_example.setStyleSheet(f"color: {MUTED}; font-size: 11px;")

        def on_prefix_changed(text: str) -> None:
            prefix_example.setText(f"Example: {format_invoice_number(text or 'INV-', 1)}")
        prefix_field.textChanged.connect(on_prefix_changed)

        default_tax = QLineEdit(str(org.default_tax_percent))
        default_discount = QLineEdit(str(org.default_discount_percent))

        def build_update() -> OrganizationUpdate | None:
            tax = _parse_decimal(default_tax.text(), "Default tax percent")
            discount = _parse_decimal(default_discount.text(), "Default discount percent")
            if tax is None or discount is None:
                QMessageBox.warning(self, "Invalid value",
                                   "Default tax and discount percent must be numbers.")
                return None
            return OrganizationUpdate(invoice_number_prefix=prefix_field.text().strip(),
                                      default_tax_percent=tax,
                                      default_discount_percent=discount)

        prefix_row = QWidget()
        prefix_layout = QVBoxLayout(prefix_row)
        prefix_layout.setContentsMargins(0, 0, 0, 0)
        prefix_layout.setSpacing(2)
        prefix_layout.addWidget(prefix_field)
        prefix_layout.addWidget(prefix_example)

        rows = [("Invoice Number Prefix *", prefix_row), ("Default Tax %", default_tax),
               ("Default Discount %", default_discount)]
        return self._build_tab(rows=rows, can_edit=can_edit, build_update=build_update)

    # -- Purchasing -------------------------------------------------------- #
    def _build_purchasing_tab(self, org: OrganizationOut, can_edit: bool) -> QWidget:
        prefix_field = QLineEdit(org.purchase_number_prefix)
        prefix_example = QLabel(
            f"Example: {format_purchase_order_number(org.purchase_number_prefix, 1)}")
        prefix_example.setStyleSheet(f"color: {MUTED}; font-size: 11px;")

        def on_prefix_changed(text: str) -> None:
            prefix_example.setText(
                f"Example: {format_purchase_order_number(text or 'PO-', 1)}")
        prefix_field.textChanged.connect(on_prefix_changed)

        def build_update() -> OrganizationUpdate:
            return OrganizationUpdate(purchase_number_prefix=prefix_field.text().strip())

        prefix_row = QWidget()
        prefix_layout = QVBoxLayout(prefix_row)
        prefix_layout.setContentsMargins(0, 0, 0, 0)
        prefix_layout.setSpacing(2)
        prefix_layout.addWidget(prefix_field)
        prefix_layout.addWidget(prefix_example)

        rows = [("Purchase Order Number Prefix *", prefix_row)]
        return self._build_tab(rows=rows, can_edit=can_edit, build_update=build_update)

    # -- Security ---------------------------------------------------------- #
    def _build_security_tab(self, org: OrganizationOut, can_edit: bool) -> QWidget:
        timeout_field = QLineEdit(str(org.session_timeout_minutes))
        min_length_field = QLineEdit(str(org.password_min_length))
        require_upper = QCheckBox("Require an uppercase letter")
        require_upper.setChecked(org.password_require_uppercase)
        require_number = QCheckBox("Require a number")
        require_number.setChecked(org.password_require_number)
        require_special = QCheckBox("Require a special character")
        require_special.setChecked(org.password_require_special_char)

        def build_update() -> OrganizationUpdate | None:
            timeout = _parse_int(timeout_field.text())
            min_length = _parse_int(min_length_field.text())
            if timeout is None or min_length is None:
                QMessageBox.warning(self, "Invalid value",
                                   "Session timeout and minimum password length must be "
                                   "whole numbers.")
                return None
            return OrganizationUpdate(
                session_timeout_minutes=timeout, password_min_length=min_length,
                password_require_uppercase=require_upper.isChecked(),
                password_require_number=require_number.isChecked(),
                password_require_special_char=require_special.isChecked())

        rows = [("Session Timeout (minutes) *", timeout_field),
               ("Minimum Password Length *", min_length_field),
               ("", require_upper), ("", require_number), ("", require_special)]
        return self._build_tab(rows=rows, can_edit=can_edit, build_update=build_update)


class _LogoEditor(QWidget):
    """Company logo: preview + upload/remove. Fetches the actual image
    bytes lazily (org.has_logo is a flag, not the bytes — see
    SqlOrganizationRepository.get_logo's docstring for why) and tracks a
    pending change locally until the surrounding tab's Save is clicked;
    apply_to() folds that pending change into the OrganizationUpdate being
    submitted.
    """
    def __init__(self, organization_service: OrganizationService, org: OrganizationOut,
                can_edit: bool):
        super().__init__()
        self._organization_service = organization_service
        self._pending_image: bytes | None = None
        self._pending_content_type: str | None = None
        self._pending_remove = False
        self._changed = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 12)
        layout.setSpacing(16)

        self._preview = QLabel("No logo")
        self._preview.setFixedSize(_LOGO_PREVIEW_SIZE, _LOGO_PREVIEW_SIZE)
        self._preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview.setStyleSheet(f"color: {MUTED}; border: 1px dashed #cbd5e1; "
                                    "border-radius: 6px; font-size: 11px;")
        layout.addWidget(self._preview)

        buttons = QVBoxLayout()
        buttons.setSpacing(8)
        upload_button = QPushButton("Upload Logo…")
        upload_button.setObjectName("ghost")
        upload_button.setCursor(Qt.CursorShape.PointingHandCursor)
        upload_button.setEnabled(can_edit)
        upload_button.clicked.connect(self._on_upload)
        buttons.addWidget(upload_button)

        remove_button = QPushButton("Remove Logo")
        remove_button.setObjectName("flat")
        remove_button.setCursor(Qt.CursorShape.PointingHandCursor)
        remove_button.setEnabled(can_edit and org.has_logo)
        remove_button.clicked.connect(self._on_remove)
        buttons.addWidget(remove_button)
        buttons.addStretch()
        layout.addLayout(buttons)
        layout.addStretch()

        if org.has_logo:
            worker = Worker(self._organization_service.get_logo)
            worker.signals.finished.connect(self._on_logo_loaded)
            QThreadPool.globalInstance().start(worker)

    def _on_logo_loaded(self, result: tuple[bytes, str | None] | None) -> None:
        if result is None or self._changed:
            return
        image_bytes, _content_type = result
        self._set_preview(image_bytes)

    def _set_preview(self, image_bytes: bytes) -> None:
        pixmap = QPixmap()
        if pixmap.loadFromData(image_bytes):
            self._preview.setPixmap(pixmap.scaled(
                _LOGO_PREVIEW_SIZE, _LOGO_PREVIEW_SIZE, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))
            self._preview.setText("")

    def _on_upload(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self, "Choose a logo image", "", "Images (*.png *.jpg *.jpeg *.gif *.bmp)")
        if not path:
            return
        with open(path, "rb") as f:
            image_bytes = f.read()
        content_type = {
            "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
            "gif": "image/gif", "bmp": "image/bmp",
        }.get(path.rsplit(".", 1)[-1].lower(), "application/octet-stream")

        self._pending_image = image_bytes
        self._pending_content_type = content_type
        self._pending_remove = False
        self._changed = True
        self._set_preview(image_bytes)

    def _on_remove(self) -> None:
        self._pending_image = None
        self._pending_content_type = None
        self._pending_remove = True
        self._changed = True
        self._preview.setPixmap(QPixmap())
        self._preview.setText("No logo")

    def apply_to(self, data: OrganizationUpdate) -> None:
        if not self._changed:
            return
        data.logo_image = self._pending_image
        data.logo_content_type = self._pending_content_type


class _BackupPanel(QWidget):
    def __init__(self, backup_service: BackupService,
                organization_service: OrganizationService, sessions: SessionManager,
                org: OrganizationOut, can_edit: bool):
        super().__init__()
        self._backup_service = backup_service
        self._organization_service = organization_service
        self._sessions = sessions
        self._current_items: list[BackupOut] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 16, 28, 24)
        layout.setSpacing(16)

        layout.addWidget(self._build_settings_card(org, can_edit))

        intro = QLabel("Backups cover the whole database and are stored as timestamped "
                       "files. Restoring replaces all current data.")
        intro.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(10)

        self._create_button = QPushButton("Create Backup")
        self._create_button.setObjectName("primary")
        self._create_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._create_button.clicked.connect(self._on_create_backup)
        self._create_button.setEnabled(self._can_backup())
        toolbar.addWidget(self._create_button)

        self._status_label = QLabel("")
        self._status_label.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        toolbar.addWidget(self._status_label)
        toolbar.addStretch()
        layout.addLayout(toolbar)

        if not self._can_backup():
            note = QLabel("You don't have permission to create or restore backups.")
            note.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
            layout.addWidget(note)

        self._async_area = AsyncContentArea(
            load=self._backup_service.list_backups, render=self._render_table,
            is_empty=lambda items: len(items) == 0,
            empty_state=EmptyStateWidget(
                "No backups yet", icon="🗄️",
                message="Create your first backup to see it here."),
            error_message="Couldn't load backup history.")
        layout.addWidget(self._async_area, stretch=1)

        row_actions = QHBoxLayout()
        row_actions.setSpacing(10)

        self._verify_button = QPushButton("Verify")
        self._verify_button.setObjectName("ghost")
        self._verify_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._verify_button.setEnabled(False)
        self._verify_button.clicked.connect(self._on_verify_selected)
        row_actions.addWidget(self._verify_button)

        self._restore_button = QPushButton("Restore…")
        self._restore_button.setObjectName("danger")
        self._restore_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._restore_button.setEnabled(False)
        self._restore_button.clicked.connect(self._on_restore_selected)
        row_actions.addWidget(self._restore_button)

        row_actions.addStretch()
        layout.addLayout(row_actions)

    # -- backup settings (location/frequency/retention) -------------------#
    def _build_settings_card(self, org: OrganizationOut, can_edit: bool) -> QWidget:
        card = QWidget()
        card.setObjectName("card")
        card.setMaximumWidth(560)
        form = QFormLayout(card)
        form.setContentsMargins(24, 20, 24, 20)
        form.setSpacing(12)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        directory_field = QLineEdit(org.backup_directory or "")
        directory_field.setPlaceholderText("Uses the installation default if left blank")
        directory_field.setEnabled(can_edit)
        form.addRow("Backup Location", directory_field)

        frequency_combo = QComboBox()
        frequency_combo.addItem("Manual only", BackupFrequency.MANUAL)
        frequency_combo.addItem("Daily", BackupFrequency.DAILY)
        frequency_combo.addItem("Weekly", BackupFrequency.WEEKLY)
        _select_by_data(frequency_combo, org.backup_frequency)
        frequency_combo.setEnabled(can_edit)
        form.addRow("Backup Frequency", frequency_combo)

        retention_field = QLineEdit(
            str(org.backup_retention_count) if org.backup_retention_count is not None else "")
        retention_field.setPlaceholderText("Leave blank to keep every backup")
        retention_field.setEnabled(can_edit)
        form.addRow("Keep Last N Backups", retention_field)

        status_label = QLabel("")
        status_label.setStyleSheet(f"color: {MUTED}; font-size: 12px;")

        container = QWidget()
        outer = QVBoxLayout(container)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(10)
        outer.addWidget(card)
        outer.addWidget(status_label)

        if can_edit:
            save_button = QPushButton("Save Backup Settings")
            save_button.setObjectName("primary")
            save_button.setCursor(Qt.CursorShape.PointingHandCursor)
            save_button.setMaximumWidth(200)

            def on_save():
                retention_text = retention_field.text().strip()
                retention = _parse_int(retention_text) if retention_text else None
                if retention_text and retention is None:
                    QMessageBox.warning(self, "Invalid value",
                                       "Retention count must be a whole number.")
                    return
                data = OrganizationUpdate(
                    backup_directory=directory_field.text().strip() or None,
                    backup_frequency=frequency_combo.currentData(),
                    backup_retention_count=retention)
                save_button.setEnabled(False)
                status_label.setText("Saving…")
                worker = Worker(self._organization_service.update_organization, data)
                worker.signals.finished.connect(
                    lambda _: (save_button.setEnabled(True), status_label.setText("Saved.")))

                def on_error(exc):
                    save_button.setEnabled(True)
                    _logger.exception("Saving backup settings failed", exc_info=exc)
                    status_label.setText("")
                    QMessageBox.critical(self, "Save failed", str(exc))

                worker.signals.error.connect(on_error)
                QThreadPool.globalInstance().start(worker)

            save_button.clicked.connect(on_save)
            outer.addWidget(save_button)

        return container

    def refresh(self) -> None:
        self._async_area.reload()

    def _can_backup(self) -> bool:
        session = self._sessions.current(now=datetime.now(timezone.utc))
        return session.is_superuser or "backup.create" in session.permissions

    def _can_restore(self) -> bool:
        session = self._sessions.current(now=datetime.now(timezone.utc))
        return session.is_superuser or "backup.restore" in session.permissions

    # -- table ------------------------------------------------------------#
    def _render_table(self, items: list[BackupOut]) -> QTableWidget:
        self._current_items = items
        table = QTableWidget(len(items), len(_BACKUP_COLUMNS))
        table.setHorizontalHeaderLabels(_BACKUP_COLUMNS)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        table.itemSelectionChanged.connect(self._on_selection_changed)

        for row, backup in enumerate(items):
            values = [
                backup.created_at.strftime("%Y-%m-%d %H:%M"), backup.filename,
                _human_size(backup.file_size_bytes), backup.status.value.title(),
                "Yes" if backup.verified else "No",
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                if col == 3 and backup.status.value == "FAILED":
                    item.setForeground(QColor(RED))
                table.setItem(row, col, item)
        return table

    def _selected_backup(self) -> BackupOut | None:
        table = self._async_area.currentWidget()
        if not isinstance(table, QTableWidget):
            return None
        rows = table.selectionModel().selectedRows() if table.selectionModel() else []
        if not rows:
            return None
        row = rows[0].row()
        if row >= len(self._current_items):
            return None
        return self._current_items[row]

    def _on_selection_changed(self) -> None:
        backup = self._selected_backup()
        can_verify = backup is not None and self._can_backup()
        self._verify_button.setEnabled(can_verify)
        self._restore_button.setEnabled(
            backup is not None and self._can_restore() and backup.status.value == "COMPLETED")

    # -- create ------------------------------------------------------------#
    def _on_create_backup(self) -> None:
        self._create_button.setEnabled(False)
        self._status_label.setText("Creating backup…")
        worker = Worker(self._backup_service.create_backup)
        worker.signals.finished.connect(self._on_backup_created)
        worker.signals.error.connect(self._on_backup_create_error)
        QThreadPool.globalInstance().start(worker)

    def _on_backup_created(self, backup: BackupOut) -> None:
        self._create_button.setEnabled(self._can_backup())
        self._status_label.setText(f"Backup created: {backup.filename}")
        self.refresh()

    def _on_backup_create_error(self, exc: Exception) -> None:
        self._create_button.setEnabled(self._can_backup())
        self._status_label.setText("")
        _logger.exception("Creating backup failed", exc_info=exc)
        QMessageBox.critical(self, "Backup failed", str(exc))
        self.refresh()  # a failed attempt is still recorded — show it in history

    # -- verify --------------------------------------------------------- #
    def _on_verify_selected(self) -> None:
        backup = self._selected_backup()
        if backup is None:
            return
        self._verify_button.setEnabled(False)
        worker = Worker(self._backup_service.verify_backup, backup.id)
        worker.signals.finished.connect(self._on_verified)
        worker.signals.error.connect(self._on_verify_error)
        QThreadPool.globalInstance().start(worker)

    def _on_verified(self, updated: BackupOut) -> None:
        self.refresh()
        if updated.verified:
            QMessageBox.information(self, "Verification complete", "Backup is valid.")
        else:
            QMessageBox.warning(self, "Verification failed",
                               "This backup did not pass verification — it may be corrupted "
                               "or incomplete. Do not rely on it for a restore.")

    def _on_verify_error(self, exc: Exception) -> None:
        self._verify_button.setEnabled(True)
        _logger.exception("Verifying backup failed", exc_info=exc)
        QMessageBox.critical(self, "Verification failed", str(exc))

    # -- restore ----------------------------------------------------------#
    def _on_restore_selected(self) -> None:
        backup = self._selected_backup()
        if backup is None:
            return
        if not backup.verified:
            QMessageBox.warning(self, "Not verified",
                               "This backup hasn't passed verification. Verify it before "
                               "restoring from it.")
            return

        confirmed = confirm_typed(
            self, "Restore database",
            "This will REPLACE ALL current data in the database with the contents of "
            f'"{backup.filename}". This cannot be undone — make sure you have a current '
            "backup of today's data before continuing.",
            required_text=backup.filename, confirm_label="Restore")
        if not confirmed:
            return

        self._restore_button.setEnabled(False)
        self._status_label.setText("Restoring…")
        worker = Worker(self._backup_service.restore_backup, backup.id)
        worker.signals.finished.connect(lambda _: self._on_restored())
        worker.signals.error.connect(self._on_restore_error)
        QThreadPool.globalInstance().start(worker)

    def _on_restored(self) -> None:
        self._status_label.setText("Restore complete.")
        QMessageBox.information(self, "Restore complete",
                               "The database has been restored from the selected backup.")
        self.refresh()

    def _on_restore_error(self, exc: Exception) -> None:
        self._restore_button.setEnabled(True)
        self._status_label.setText("")
        _logger.exception("Restoring backup failed", exc_info=exc)
        QMessageBox.critical(self, "Restore failed", str(exc))
