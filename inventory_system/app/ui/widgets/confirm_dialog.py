"""Reusable confirmation dialogs — one place controlling how every "are you
sure?" prompt in the app looks and behaves (Escape/click-outside cancels,
Enter confirms the default button), instead of ad hoc QMessageBox calls
scattered across pages.
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)


def confirm(parent: QWidget, title: str, message: str, confirm_label: str = "Confirm",
           danger: bool = False) -> bool:
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(message)
    box.setIcon(QMessageBox.Icon.Warning if danger else QMessageBox.Icon.Question)
    confirm_button = box.addButton(confirm_label, QMessageBox.ButtonRole.AcceptRole)
    confirm_button.setObjectName("danger" if danger else "primary")
    cancel_button = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
    cancel_button.setObjectName("flat")
    box.setDefaultButton(confirm_button)
    box.exec()
    return box.clickedButton() is confirm_button


def confirm_typed(parent: QWidget, title: str, message: str, required_text: str,
                  confirm_label: str = "Confirm") -> bool:
    """For actions destructive enough that a plain Yes/No is too easy to
    click through on reflex (e.g. restoring the database over everything
    currently in it) — the confirm button stays disabled until the user
    types ``required_text`` exactly, forcing a deliberate pause.
    """
    dialog = QDialog(parent)
    dialog.setWindowTitle(title)
    dialog.setModal(True)
    dialog.setMinimumWidth(380)

    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(20, 20, 20, 20)
    layout.setSpacing(12)
    message_label = QLabel(message)
    message_label.setWordWrap(True)
    layout.addWidget(message_label)
    layout.addWidget(QLabel(f'Type "{required_text}" below to confirm:'))

    input_field = QLineEdit()
    layout.addWidget(input_field)

    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                               | QDialogButtonBox.StandardButton.Cancel)
    ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
    ok_button.setText(confirm_label)
    ok_button.setObjectName("danger")
    ok_button.setEnabled(False)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)

    input_field.textChanged.connect(
        lambda text: ok_button.setEnabled(text == required_text))
    input_field.setFocus(Qt.FocusReason.OtherFocusReason)

    return dialog.exec() == QDialog.DialogCode.Accepted
