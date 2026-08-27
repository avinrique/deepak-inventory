"""Add/Edit/View user dialog — same shape as ProductFormDialog: collects raw
field values, validates them locally for instant feedback, then hands them
to UserService and displays whatever it returns or raises.

Client-side validation reuses the exact same pure, I/O-free functions the
server runs (app.domain.user.validate_user, app.domain.security_policy.
validate_password) rather than re-implementing the rules — so "valid email",
"required fields", and "strong password" mean the same thing here as they do
server-side, and there's only ever one place those rules are defined.
Uniqueness (email/username) can't be checked locally without a round trip,
so that's still enforced server-side (DuplicateEmailError/
DuplicateUsernameError below) — the same Worker/QThreadPool call already
keeps that off the GUI thread.

Role, initial password, and status are only collected on create —
UserCreate requires them; UserUpdate deliberately doesn't carry role_id
(that goes through the dedicated Change Role dialog, gated by its own
users.manage_roles permission), a password (ChangePasswordDialog/
reset_password own that — the current password is never shown or
editable here), or is_active (that goes through the dedicated Activate/
Deactivate actions, which also enforce the Owner-protection rule).

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
from app.domain.security_policy import PasswordPolicy, validate_password
from app.domain.user import validate_user
from app.schemas.user import RoleOut, UserSummaryOut, UserUpdate
from app.services.user_service import UserService
from app.ui.theme import MUTED, RED, STYLESHEET
from app.ui.widgets.responsive import constrain_dialog
from app.workers.base_worker import Worker

DEFAULT_PASSWORD_POLICY = PasswordPolicy(min_length=8, require_uppercase=False,
                                         require_number=False, require_special_char=False)


def _policy_hint(policy: PasswordPolicy) -> str:
    requirements = [f"at least {policy.min_length} characters"]
    if policy.require_uppercase:
        requirements.append("an uppercase letter")
    if policy.require_number:
        requirements.append("a number")
    if policy.require_special_char:
        requirements.append("a special character")
    return "Must contain " + ", ".join(requirements) + "."


class UserFormDialog(QDialog):
    def __init__(self, user_service: UserService, roles: list[RoleOut],
                organization_id, user: UserSummaryOut | None = None,
                read_only: bool = False, parent=None,
                password_policy: PasswordPolicy = DEFAULT_PASSWORD_POLICY):
        super().__init__(parent)
        self._user_service = user_service
        self._user = user
        self._organization_id = organization_id
        self._read_only = read_only
        self._password_policy = password_policy
        self.setWindowTitle("View User" if read_only else
                            ("Edit User" if user else "Add User"))
        constrain_dialog(self, 420)
        self.setStyleSheet(STYLESHEET)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(10)

        form = QFormLayout()
        form.setSpacing(8)

        self._full_name = QLineEdit(user.full_name if user else "")
        form.addRow("Full Name *", self._full_name)

        self._username = QLineEdit(user.username if user else "")
        if user is None:
            self._username.setPlaceholderText("Auto-generated from email if left blank")
        form.addRow("Username" + (" *" if user is not None else ""), self._username)

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

            self._status = QComboBox()
            self._status.addItem("Active", True)
            self._status.addItem("Inactive", False)
            form.addRow("Status", self._status)
            editable_widgets.append(self._status)

            self._password = QLineEdit()
            self._password.setEchoMode(QLineEdit.EchoMode.Password)
            form.addRow("Password *", self._password)
            editable_widgets.append(self._password)

            hint = QLabel(_policy_hint(password_policy))
            hint.setWordWrap(True)
            hint.setStyleSheet(f"color: {MUTED}; font-size: 11px;")
            form.addRow("", hint)

            self._confirm_password = QLineEdit()
            self._confirm_password.setEchoMode(QLineEdit.EchoMode.Password)
            form.addRow("Confirm Password *", self._confirm_password)
            editable_widgets.append(self._confirm_password)
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

    def _validate_locally(self, *, full_name: str, email: str, username: str | None,
                          phone: str | None) -> list[str]:
        is_create = self._user is None
        errors = []
        # A blank username is only a valid, deliberate choice on create
        # (derived from the email server-side) — on edit there's already a
        # real username, so clearing it is a mistake, not "auto-derive".
        if not is_create and not username:
            errors.append("Username is required.")

        errors.extend(validate_user(full_name=full_name, email=email, username=username,
                                    phone=phone))

        if is_create:
            if self._role.currentData() is None:
                errors.append("Select a role.")

            password = self._password.text()
            confirm_password = self._confirm_password.text()
            if not password:
                errors.append("Password is required.")
            elif not confirm_password:
                errors.append("Confirm the password.")
            elif password != confirm_password:
                errors.append("Password and confirmation do not match.")
            else:
                errors.extend(validate_password(password, self._password_policy))

        return errors

    def _submit(self) -> None:
        self._error_label.hide()
        full_name = self._full_name.text().strip()
        email = self._email.text().strip()
        username_text = self._username.text().strip()
        username = username_text or None
        phone = self._phone.text().strip() or None

        errors = self._validate_locally(full_name=full_name, email=email,
                                        username=username, phone=phone)
        if errors:
            self._show_error(" ".join(errors))
            return

        self._set_busy(True)
        if self._user is None:
            worker = Worker(self._user_service.create_user, email=email,
                            full_name=full_name, initial_password=self._password.text(),
                            organization_id=self._organization_id,
                            role_id=self._role.currentData(), username=username,
                            phone=phone, is_active=self._status.currentData())
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
        # Only expected, already-friendly AppError messages are shown
        # verbatim — anything else (a raw SQLAlchemy/DB error, a bug) falls
        # through to a generic message so no technical detail ever reaches
        # the user.
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
