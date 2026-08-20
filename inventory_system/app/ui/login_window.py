"""Login window — the entrypoint before MainWindow exists. Runs
AuthService.login on a background thread (Argon2id hashing is real CPU
work, not instant) so the window stays responsive, with a visible loading
state on the button and an inline error state on failure.

Emits login_succeeded(Session) rather than reaching into app.main itself —
main.py owns the transition to MainWindow, this window only knows about
authenticating.
"""
from PySide6.QtCore import Qt, QThreadPool, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QComboBox,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.core.exceptions import (
    AccountLockedError,
    AmbiguousOrganizationError,
    InvalidCredentialsError,
)
from app.security.session import Session
from app.services.auth_service import AuthService
from app.ui.theme import CONTENT_BG, MUTED, RED, STYLESHEET, TEXT
from app.workers.base_worker import Worker


class LoginWindow(QWidget):
    login_succeeded = Signal(object)  # Session

    def __init__(self, auth_service: AuthService):
        super().__init__()
        self._auth_service = auth_service
        self.setWindowTitle("Inventory Management — Log In")
        # Minimum, not fixed: the organization picker (shown only for
        # multi-org accounts) and longer error/localized text need to be
        # able to grow the window rather than clip within a hard cap.
        self.setMinimumSize(420, 480)
        self.resize(420, 480)
        self.setObjectName("loginWindow")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(STYLESHEET + f"QWidget#loginWindow {{ background: {CONTENT_BG}; }}")

        outer = QVBoxLayout(self)
        outer.setAlignment(Qt.AlignmentFlag.AlignCenter)

        card = QWidget()
        card.setObjectName("card")
        card.setFixedWidth(340)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(32, 32, 32, 32)
        card_layout.setSpacing(6)

        brand = QLabel("Inventory")
        brand.setStyleSheet(f"font-size: 20px; font-weight: 700; color: {TEXT};")
        card_layout.addWidget(brand)
        subtitle = QLabel("Sign in to your account")
        subtitle.setStyleSheet(f"font-size: 12px; color: {MUTED}; margin-bottom: 18px;")
        card_layout.addWidget(subtitle)

        card_layout.addWidget(QLabel("Email"))
        self._email = QLineEdit()
        self._email.setPlaceholderText("you@company.com")
        card_layout.addWidget(self._email)
        card_layout.addSpacing(10)

        card_layout.addWidget(QLabel("Password"))
        self._password = QLineEdit()
        self._password.setEchoMode(QLineEdit.EchoMode.Password)
        card_layout.addWidget(self._password)
        card_layout.addSpacing(4)

        # Shown only after AuthService.login raises AmbiguousOrganizationError
        # (an account with memberships in >1 organization and no default) —
        # hidden the rest of the time so the common single-organization
        # login stays a two-field form.
        self._org_label = QLabel("Organization")
        self._org_label.hide()
        card_layout.addWidget(self._org_label)
        self._org_combo = QComboBox()
        self._org_combo.hide()
        card_layout.addWidget(self._org_combo)
        card_layout.addSpacing(4)

        self._error_label = QLabel("")
        self._error_label.setWordWrap(True)
        self._error_label.setStyleSheet(f"color: {RED}; font-size: 12px;")
        self._error_label.hide()
        card_layout.addWidget(self._error_label)
        card_layout.addSpacing(14)

        self._submit_button = QPushButton("Log In")
        self._submit_button.setObjectName("primary")
        self._submit_button.setDefault(True)
        self._submit_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._submit_button.clicked.connect(self._submit)
        card_layout.addWidget(self._submit_button)

        outer.addWidget(card, alignment=Qt.AlignmentFlag.AlignCenter)

        self._email.returnPressed.connect(self._submit)
        self._password.returnPressed.connect(self._submit)
        self._email.textChanged.connect(self._hide_org_picker)
        self._email.setFocus()

    def _set_busy(self, busy: bool) -> None:
        self._submit_button.setEnabled(not busy)
        self._submit_button.setText("Logging in…" if busy else "Log In")
        self._email.setEnabled(not busy)
        self._password.setEnabled(not busy)

    def _submit(self) -> None:
        email = self._email.text().strip()
        password = self._password.text()
        self._error_label.hide()
        if not email or not password:
            self._show_error("Enter your email and password.")
            return

        organization_id = None
        if self._org_combo.isVisible():
            if self._org_combo.currentIndex() < 0:
                self._show_error("Select an organization to log into.")
                return
            organization_id = self._org_combo.currentData()

        self._set_busy(True)
        worker = Worker(self._auth_service.login, email, password, organization_id)
        worker.signals.finished.connect(self._on_success)
        worker.signals.error.connect(self._on_error)
        QThreadPool.globalInstance().start(worker)

    def _on_success(self, session: Session) -> None:
        self._set_busy(False)
        self._password.clear()
        self._hide_org_picker()
        self.login_succeeded.emit(session)

    def _on_error(self, exc: Exception) -> None:
        self._set_busy(False)
        if isinstance(exc, InvalidCredentialsError):
            self._show_error("Incorrect email or password.")
        elif isinstance(exc, AmbiguousOrganizationError):
            self._show_org_picker(exc.organizations)
        elif isinstance(exc, AccountLockedError):
            self._show_error(str(exc))
        else:
            self._show_error("Something went wrong logging in. Please try again.")

    def _show_org_picker(self, organizations: list[tuple]) -> None:
        self._org_combo.clear()
        for organization_id, name in organizations:
            self._org_combo.addItem(name, organization_id)
        if self._org_combo.count() == 0:
            # AuthService found no resolvable organization for any
            # membership (e.g. all referenced organizations were deleted) —
            # nothing a picker can offer, so surface the original message.
            self._show_error(str(AmbiguousOrganizationError()))
            return
        self._org_label.show()
        self._org_combo.show()
        self._org_combo.setFocus()
        self._show_error("This account belongs to multiple organizations — choose one below.")

    def _hide_org_picker(self) -> None:
        self._org_label.hide()
        self._org_combo.hide()
        self._org_combo.clear()

    def _show_error(self, message: str) -> None:
        self._error_label.setText(message)
        self._error_label.show()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt override
        if event.key() == Qt.Key.Key_Escape:
            self._email.clear()
            self._password.clear()
            self._error_label.hide()
        super().keyPressEvent(event)
