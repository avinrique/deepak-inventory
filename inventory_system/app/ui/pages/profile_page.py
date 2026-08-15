"""The current user's own Profile: view/edit their basic info, see their
role and account status, change their password, and log out. No business
logic here — every action calls AuthService/UserService on a background
Worker and renders whatever comes back, same as every other page.

Editing here always goes through UserService.update_own_profile, never
update_user — the former needs no permission beyond being logged in (you
can always edit yourself), the latter is the admin path gated by
users.update for editing *someone else*, reached from the Users page, not
here. See update_own_profile's docstring in app.services.user_service.

Change Password reuses ChangePasswordDialog verbatim (the same dialog
MainWindow's header menu opens) rather than reimplementing current/new/
confirm-password fields a second time. Log Out only emits a signal;
MainWindow (already the one place that owns the confirm-dialog + logout +
session_ended sequence, for the header menu's Log Out too) is what
actually acts on it — this page has no logout logic of its own to keep in
sync with that.
"""
import logging
from typing import NamedTuple

from PySide6.QtCore import QThreadPool, Qt, Signal
from PySide6.QtWidgets import (
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.core.exceptions import (
    DuplicateEmailError,
    DuplicateUsernameError,
    UserNotFoundError,
    UserValidationError,
)
from app.domain.user import validate_user
from app.schemas.user import MembershipOut, UserOut, UserUpdate
from app.services.auth_service import AuthService
from app.services.user_service import UserService
from app.ui.theme import GREEN, MUTED, RED
from app.ui.widgets.async_content import AsyncContentArea
from app.ui.widgets.avatar import AvatarLabel
from app.ui.widgets.change_password_dialog import ChangePasswordDialog
from app.ui.widgets.confirm_dialog import confirm
from app.ui.widgets.page_header import PageHeader
from app.ui.widgets.states import EmptyStateWidget
from app.workers.base_worker import Worker

_logger = logging.getLogger(__name__)


class _ProfileData(NamedTuple):
    user: UserOut
    membership: MembershipOut | None


class ProfilePage(QWidget):
    logout_requested = Signal()

    def __init__(self, auth_service: AuthService, user_service: UserService):
        super().__init__()
        self._auth_service = auth_service
        self._user_service = user_service
        self._current_user: UserOut | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(PageHeader("My Profile", "View and manage your account."))

        self._async_area = AsyncContentArea(
            load=self._load, render=self._render,
            is_empty=lambda _data: False,
            empty_state=EmptyStateWidget("No profile", icon="🙍"),
            error_message="Couldn't load your profile.")
        layout.addWidget(self._async_area, stretch=1)

    def refresh(self) -> None:
        self._async_area.reload()

    def _load(self) -> _ProfileData:
        return _ProfileData(user=self._auth_service.get_current_user(),
                            membership=self._auth_service.get_current_membership())

    # -- rendering ------------------------------------------------------ #
    def _render(self, data: _ProfileData) -> QWidget:
        self._current_user = data.user
        role_name = data.membership.role_name if data.membership else "Superuser"

        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(28, 16, 28, 24)
        outer.setSpacing(20)

        outer.addWidget(self._build_identity_card(data.user, role_name))
        outer.addWidget(self._build_edit_card(data.user))
        outer.addWidget(self._build_security_card())
        outer.addStretch()
        return page

    def _build_identity_card(self, user: UserOut, role_name: str) -> QWidget:
        card = QWidget()
        card.setObjectName("card")
        layout = QHBoxLayout(card)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(16)

        layout.addWidget(AvatarLabel(user.full_name, size=56, font_size=18))

        text_box = QVBoxLayout()
        text_box.setSpacing(4)
        name_label = QLabel(user.full_name)
        name_label.setStyleSheet("font-size: 16px; font-weight: 700;")
        text_box.addWidget(name_label)

        meta = QHBoxLayout()
        meta.setSpacing(10)
        meta.addWidget(self._pill(role_name))
        status_text = "Active" if user.is_active else "Inactive"
        status_pill = self._pill(status_text, color=GREEN if user.is_active else RED)
        meta.addWidget(status_pill)
        meta.addStretch()
        text_box.addLayout(meta)

        last_login = (user.last_login_at.strftime("%Y-%m-%d %H:%M")
                     if user.last_login_at else "Never")
        info = QLabel(f"Member since {user.created_at.strftime('%Y-%m-%d')}  ·  "
                     f"Last login {last_login}")
        info.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        text_box.addWidget(info)

        layout.addLayout(text_box, stretch=1)
        return card

    @staticmethod
    def _pill(text: str, color: str = MUTED) -> QLabel:
        pill = QLabel(text)
        pill.setStyleSheet(f"""
            color: {color}; font-size: 11px; font-weight: 700;
            border: 1px solid {color}; border-radius: 9px; padding: 2px 10px;
        """)
        return pill

    def _build_edit_card(self, user: UserOut) -> QWidget:
        card = QWidget()
        card.setObjectName("card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)

        title = QLabel("Profile Information")
        title.setStyleSheet("font-size: 13px; font-weight: 700;")
        layout.addWidget(title)

        form = QFormLayout()
        form.setSpacing(8)

        self._full_name = QLineEdit(user.full_name)
        form.addRow("Full Name *", self._full_name)

        self._username = QLineEdit(user.username)
        form.addRow("Username *", self._username)

        self._email = QLineEdit(user.email)
        form.addRow("Email *", self._email)

        self._phone = QLineEdit(user.phone or "")
        form.addRow("Phone", self._phone)

        layout.addLayout(form)

        self._error_label = QLabel("")
        self._error_label.setWordWrap(True)
        self._error_label.setStyleSheet(f"color: {RED}; font-size: 12px;")
        self._error_label.hide()
        layout.addWidget(self._error_label)

        self._save_button = QPushButton("Save Changes")
        self._save_button.setObjectName("primary")
        self._save_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._save_button.clicked.connect(self._save)
        layout.addWidget(self._save_button, alignment=Qt.AlignmentFlag.AlignLeft)

        return card

    def _build_security_card(self) -> QWidget:
        card = QWidget()
        card.setObjectName("card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)

        title = QLabel("Security")
        title.setStyleSheet("font-size: 13px; font-weight: 700;")
        layout.addWidget(title)

        description = QLabel("Change your password, or sign out of this session.")
        description.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        layout.addWidget(description)

        buttons = QHBoxLayout()
        buttons.setSpacing(10)

        change_password_button = QPushButton("Change Password")
        change_password_button.setObjectName("ghost")
        change_password_button.setCursor(Qt.CursorShape.PointingHandCursor)
        change_password_button.clicked.connect(self._open_change_password)
        buttons.addWidget(change_password_button)

        logout_button = QPushButton("Log Out")
        logout_button.setObjectName("danger")
        logout_button.setCursor(Qt.CursorShape.PointingHandCursor)
        logout_button.clicked.connect(self._on_logout_clicked)
        buttons.addWidget(logout_button)

        buttons.addStretch()
        layout.addLayout(buttons)
        return card

    # -- actions ---------------------------------------------------------- #
    def _open_change_password(self) -> None:
        dialog = ChangePasswordDialog(self._auth_service, parent=self)
        if dialog.exec():
            QMessageBox.information(self, "Password Changed",
                                    "Your password has been changed.")

    def _on_logout_clicked(self) -> None:
        # No local confirm() here — MainWindow's slot for this signal
        # already confirms (it's the same slot the header's Log Out uses).
        # Confirming twice would just be annoying, not safer.
        self.logout_requested.emit()

    def _set_busy(self, busy: bool) -> None:
        self._save_button.setEnabled(not busy)
        self._save_button.setText("Saving…" if busy else "Save Changes")

    def _save(self) -> None:
        self._error_label.hide()
        full_name = self._full_name.text().strip()
        username = self._username.text().strip()
        email = self._email.text().strip()
        phone = self._phone.text().strip() or None

        errors = validate_user(full_name=full_name, email=email, username=username,
                               phone=phone)
        if errors:
            self._show_error(" ".join(errors))
            return

        data = UserUpdate(full_name=full_name, username=username, email=email, phone=phone)
        self._set_busy(True)
        worker = Worker(self._user_service.update_own_profile, data)
        worker.signals.finished.connect(self._on_save_success)
        worker.signals.error.connect(self._on_save_error)
        QThreadPool.globalInstance().start(worker)

    def _on_save_success(self, _result: UserOut) -> None:
        self._set_busy(False)
        self.refresh()

    def _on_save_error(self, exc: Exception) -> None:
        self._set_busy(False)
        if isinstance(exc, UserValidationError):
            self._show_error(" ".join(exc.errors))
        elif isinstance(exc, (DuplicateEmailError, DuplicateUsernameError, UserNotFoundError)):
            self._show_error(str(exc))
        else:
            _logger.exception("Saving profile failed", exc_info=exc)
            self._show_error("Something went wrong saving your profile. Please try again.")

    def _show_error(self, message: str) -> None:
        self._error_label.setText(message)
        self._error_label.show()
