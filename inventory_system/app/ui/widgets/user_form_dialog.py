"""Add/Edit/View user dialog — same shape as ProductFormDialog: collects raw
field values, hands them to UserService, and displays whatever it returns or
raises. Field validation (app.domain.user.validate_user), duplicate checks,
and password-policy checks all happen in UserService, not here.

Role/initial password are only collected on create — UserCreate requires
them, UserUpdate deliberately doesn't carry role_id (that goes through the
dedicated Change Role action/dialog, gated by its own users.manage_roles
permission) or a password (ChangePasswordDialog/reset_password own that).

read_only=True turns this into a "View User" screen: every field disabled,
no Save button — same convention as ProductFormDialog.
"""
from PySide6.QtCore import QThreadPool, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from app.core.exceptions import (
    DuplicateEmailError,
    DuplicateUsernameError,
    PasswordPolicyViolationError,
    RoleNotFoundError,
    UserNotFoundError,
    UserValidationError,
)
from app.schemas.user import RoleOut, UserSummaryOut, UserUpdate
from app.services.user_service import UserService
from app.ui.theme import MUTED, RED, STYLESHEET
from app.workers.base_worker import Worker


class UserFormDialog(QDialog):
    def __init__(self, user_service: UserService, roles: list[RoleOut],
                organization_id, user: UserSummaryOut | None = None,
                read_only: bool = False, parent=None):
        super().__init__(parent)
        self._user_service = user_service
        self._user = user
        self._organization_id = organization_id
        self._read_only = read_only
        self.setWindowTitle("View User" if read_only else
                            ("Edit User" if user else "Add User"))
        self.setMinimumWidth(420)
        self.setStyleSheet(STYLESHEET)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(10)

        form = QFormLayout()
        form.setSpacing(8)

        self._full_name = QLineEdit(user.full_name if user else "")
        form.addRow("Full Name *", self._full_name)

        self._username = QLineEdit(user.username if user else "")
        self._username.setPlaceholderText("Auto-generated from email if left blank")
        form.addRow("Username", self._username)

        self._email = QLineEdit(user.email if user else "")
        form.addRow("Email *", self._email)

        self._phone = QLineEdit(user.phone if user and user.phone else "")
        form.addRow("Phone", self._phone)

        editable_widgets = [self._full_name, self._username, self._email, self._phone]

        if user is None:
            self._role = QComboBox()
            for role in roles:
                self._role.addItem(role.name, role.id)
            form.addRow("Role *", self._role)
            editable_widgets.append(self._role)

            self._initial_password = QLineEdit()
            self._initial_password.setEchoMode(QLineEdit.EchoMode.Password)
            form.addRow("Initial Password *", self._initial_password)
            editable_widgets.append(self._initial_password)
        else:
            role_label = QLabel(user.role_name)
            role_label.setStyleSheet(f"color: {MUTED};")
            form.addRow("Role", role_label)

        if user is not None:
            status_label = QLabel("Active" if user.is_active else "Inactive")
            status_label.setStyleSheet(f"color: {MUTED};")
            form.addRow("Status", status_label)

            last_login = (user.last_login_at.strftime("%Y-%m-%d %H:%M")
                         if user.last_login_at else "Never")
            last_login_label = QLabel(last_login)
            last_login_label.setStyleSheet(f"color: {MUTED};")
            form.addRow("Last Login", last_login_label)

            created_label = QLabel(user.created_at.strftime("%Y-%m-%d %H:%M"))
            created_label.setStyleSheet(f"color: {MUTED};")
            form.addRow("Created", created_label)

        layout.addLayout(form)

        self._error_label = QLabel("")
        self._error_label.setWordWrap(True)
        self._error_label.setStyleSheet(f"color: {RED}; font-size: 12px;")
        self._error_label.hide()
        layout.addWidget(self._error_label)

        if read_only:
            for widget in editable_widgets:
                widget.setEnabled(False)
            close_button = QPushButton("Close")
            close_button.setObjectName("primary")
            close_button.clicked.connect(self.reject)
            layout.addWidget(close_button)
        else:
            self._save_button = QPushButton("Save User")
            self._save_button.setObjectName("primary")
            self._save_button.setCursor(Qt.CursorShape.PointingHandCursor)
            self._save_button.clicked.connect(self._submit)
            layout.addWidget(self._save_button)

    def _set_busy(self, busy: bool) -> None:
        self._save_button.setEnabled(not busy)
        self._save_button.setText("Saving…" if busy else "Save User")

    def _submit(self) -> None:
        self._error_label.hide()
        full_name = self._full_name.text().strip()
        email = self._email.text().strip()
        username = self._username.text().strip() or None
        phone = self._phone.text().strip() or None

        self._set_busy(True)
        if self._user is None:
            role_id = self._role.currentData()
            if role_id is None:
                self._set_busy(False)
                self._show_error("Select a role.")
                return
            initial_password = self._initial_password.text()
            if not initial_password:
                self._set_busy(False)
                self._show_error("Enter an initial password.")
                return
            worker = Worker(self._user_service.create_user, email=email,
                            full_name=full_name, initial_password=initial_password,
                            organization_id=self._organization_id, role_id=role_id,
                            username=username, phone=phone)
        else:
            data = UserUpdate(email=email, username=username, full_name=full_name,
                              phone=phone)
            worker = Worker(self._user_service.update_user, self._user.id, data)

        worker.signals.finished.connect(self._on_success)
        worker.signals.error.connect(self._on_error)
        QThreadPool.globalInstance().start(worker)

    def _on_success(self, _result) -> None:
        self._set_busy(False)
        self.accept()

    def _on_error(self, exc: Exception) -> None:
        self._set_busy(False)
        if isinstance(exc, (UserValidationError, PasswordPolicyViolationError)):
            self._show_error(" ".join(exc.errors))
        elif isinstance(exc, (DuplicateEmailError, DuplicateUsernameError,
                              UserNotFoundError, RoleNotFoundError)):
            self._show_error(str(exc))
        else:
            self._show_error("Something went wrong saving this user. Please try again.")

    def _show_error(self, message: str) -> None:
        self._error_label.setText(message)
        self._error_label.show()
