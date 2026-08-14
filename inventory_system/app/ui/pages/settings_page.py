"""Settings page — the organization's company profile (name, legal name,
tax id, address, contact details, invoice number prefix, negative-stock
policy). This is the "Settings" the invoice/PDF system pulls company
information from — see app.reports.sales_invoice_pdf — so it exists as
real, editable data now rather than a placeholder.

Read-only for anyone without settings.manage (the Save button is disabled,
not hidden, so a viewer can still see the company profile); OrganizationService
enforces the same permission independently, so a client-side bypass of the
disabled state still wouldn't let a write through.
"""
import logging
from datetime import datetime, timezone

from PySide6.QtCore import QThreadPool, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.schemas.organization import OrganizationOut, OrganizationUpdate
from app.security.session import SessionManager
from app.services.organization_service import OrganizationService
from app.ui.theme import MUTED
from app.ui.widgets.async_content import AsyncContentArea
from app.ui.widgets.page_header import PageHeader
from app.ui.widgets.states import EmptyStateWidget
from app.workers.base_worker import Worker

_logger = logging.getLogger(__name__)


class SettingsPage(QWidget):
    def __init__(self, organization_service: OrganizationService, sessions: SessionManager):
        super().__init__()
        self._organization_service = organization_service
        self._sessions = sessions

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(PageHeader("Settings", "Your organization's company profile."))

        self._async_area = AsyncContentArea(
            load=self._organization_service.get_current_organization,
            render=self._render_form,
            is_empty=lambda org: False,
            empty_state=EmptyStateWidget("No organization", icon="🏢"),
            error_message="Couldn't load organization settings.")
        layout.addWidget(self._async_area, stretch=1)

    def refresh(self) -> None:
        self._async_area.reload()

    def _can_edit(self) -> bool:
        session = self._sessions.current(now=datetime.now(timezone.utc))
        return session.is_superuser or "settings.manage" in session.permissions

    def _render_form(self, org: OrganizationOut) -> QWidget:
        container = QWidget()
        container.setContentsMargins(28, 12, 28, 24)
        outer = QVBoxLayout(container)
        outer.setContentsMargins(28, 12, 28, 24)
        outer.setSpacing(16)

        card = QWidget()
        card.setObjectName("card")
        card.setMaximumWidth(560)
        form = QFormLayout(card)
        form.setContentsMargins(24, 20, 24, 20)
        form.setSpacing(12)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        can_edit = self._can_edit()

        fields = {
            "name": QLineEdit(org.name),
            "legal_name": QLineEdit(org.legal_name or ""),
            "tax_id": QLineEdit(org.tax_id or ""),
            "address": QLineEdit(org.address or ""),
            "phone": QLineEdit(org.phone or ""),
            "email": QLineEdit(org.email or ""),
            "website": QLineEdit(org.website or ""),
            "invoice_number_prefix": QLineEdit(org.invoice_number_prefix),
        }
        labels = {
            "name": "Company Name", "legal_name": "Legal Name", "tax_id": "Tax ID",
            "address": "Address", "phone": "Phone", "email": "Email", "website": "Website",
            "invoice_number_prefix": "Invoice Number Prefix",
        }
        for key, widget in fields.items():
            widget.setEnabled(can_edit)
            form.addRow(labels[key], widget)

        allow_negative = QCheckBox("Allow inventory to go negative")
        allow_negative.setChecked(org.allow_negative_stock)
        allow_negative.setEnabled(can_edit)
        form.addRow("", allow_negative)

        outer.addWidget(card)

        status_label = QLabel("" if can_edit else
                              "You don't have permission to edit organization settings.")
        status_label.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        outer.addWidget(status_label)

        if can_edit:
            save_button = QPushButton("Save Changes")
            save_button.setObjectName("primary")
            save_button.setCursor(Qt.CursorShape.PointingHandCursor)
            save_button.setMaximumWidth(160)

            def on_save():
                data = OrganizationUpdate(
                    name=fields["name"].text().strip(),
                    legal_name=fields["legal_name"].text().strip() or None,
                    tax_id=fields["tax_id"].text().strip() or None,
                    address=fields["address"].text().strip() or None,
                    phone=fields["phone"].text().strip() or None,
                    email=fields["email"].text().strip() or None,
                    website=fields["website"].text().strip() or None,
                    invoice_number_prefix=fields["invoice_number_prefix"].text().strip(),
                    allow_negative_stock=allow_negative.isChecked())
                save_button.setEnabled(False)
                status_label.setText("Saving…")
                worker = Worker(self._organization_service.update_organization, data)
                worker.signals.finished.connect(lambda _: self._on_saved(status_label,
                                                                         save_button))
                worker.signals.error.connect(lambda exc: self._on_save_error(exc, status_label,
                                                                             save_button))
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
        _logger.exception("Saving organization settings failed", exc_info=exc)
        status_label.setText("")
        QMessageBox.critical(self, "Save failed", str(exc))
