"""Change-password dialog. Used two ways: self-service from the user menu
(cancellable), and mandatory right after an admin-initiated reset — see
AuthService.change_password / User.must_change_password, built earlier but
never wired to any UI until now.
"""
from PySide6.QtCore import QThreadPool, Qt
from PySide6.QtWidgets import QDialog, QLabel, QLineEdit, QPushButton, QVBoxLayout

from app.core.exceptions import InvalidCredentialsError
from app.services.auth_service import AuthService
from app.ui.theme import MUTED, RED, STYLESHEET
from app.ui.widgets.responsive import constrain_dialog
from app.workers.base_worker import Worker


class ChangePasswordDialog(QDialog):
    def __init__(self, auth_service: AuthService, parent=None, mandatory: bool = False):
        super().__init__(parent)
        self._auth_service = auth_service
        self.setWindowTitle("Change Password")
        constrain_dialog(self, 360)
        self.setStyleSheet(STYLESHEET)
        if mandatory:
            self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowCloseButtonHint)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(8)

        if mandatory:
            notice = QLabel("Your password was reset by an administrator. "
                            "Choose a new password to continue.")
            notice.setWordWrap(True)
            notice.setStyleSheet(f"color: {MUTED}; font-size: 12px; margin-bottom: 8px;")
            layout.addWidget(notice)

        layout.addWidget(QLabel("Current password"))
        self._old = QLineEdit()
        self._old.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addWidget(self._old)

        layout.addWidget(QLabel("New password"))
        self._new = QLineEdit()
        self._new.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addWidget(self._new)

        layout.addWidget(QLabel("Confirm new password"))
        self._confirm = QLineEdit()
        self._confirm.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addWidget(self._confirm)

        self._error_label = QLabel("")
        self._error_label.setWordWrap(True)
        self._error_label.setStyleSheet(f"color: {RED}; font-size: 12px;")
        self._error_label.hide()
        layout.addWidget(self._error_label)

        self._submit_button = QPushButton("Change Password")
        self._submit_button.setObjectName("primary")
        self._submit_button.setDefault(True)
        self._submit_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._submit_button.clicked.connect(self._submit)
        layout.addWidget(self._submit_button)

        if not mandatory:
            cancel_button = QPushButton("Cancel")
            cancel_button.setObjectName("flat")
            cancel_button.clicked.connect(self.reject)
            layout.addWidget(cancel_button)

    def _set_busy(self, busy: bool) -> None:
        self._submit_button.setEnabled(not busy)
        self._submit_button.setText("Changing…" if busy else "Change Password")

    def _submit(self) -> None:
        old, new, confirm = self._old.text(), self._new.text(), self._confirm.text()
        self._error_label.hide()
        if not old or not new:
            return self._show_error("Fill in both password fields.")
        if new != confirm:
            return self._show_error("New password and confirmation don't match.")
        if len(new) < 8:
            return self._show_error("New password must be at least 8 characters.")

        self._set_busy(True)
        worker = Worker(self._auth_service.change_password, old, new)
        worker.signals.finished.connect(lambda _: (self._set_busy(False), self.accept()))
        worker.signals.error.connect(self._on_error)
        QThreadPool.globalInstance().start(worker)

    def _on_error(self, exc: Exception) -> None:
        self._set_busy(False)
        if isinstance(exc, InvalidCredentialsError):
            self._show_error("Current password is incorrect.")
        else:
            self._show_error("Something went wrong. Please try again.")

    def _show_error(self, message: str) -> None:
        self._error_label.setText(message)
        self._error_label.show()
