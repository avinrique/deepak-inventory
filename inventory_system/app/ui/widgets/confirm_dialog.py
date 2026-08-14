"""Reusable confirmation dialog — one place controlling how every "are you
sure?" prompt in the app looks and behaves (Escape/click-outside cancels,
Enter confirms the default button), instead of ad hoc QMessageBox calls
scattered across pages.
"""
from PySide6.QtWidgets import QMessageBox, QWidget


def confirm(parent: QWidget, title: str, message: str, confirm_label: str = "Confirm",
           danger: bool = False) -> bool:
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(message)
    box.setIcon(QMessageBox.Icon.Warning if danger else QMessageBox.Icon.Question)
    confirm_button = box.addButton(confirm_label, QMessageBox.ButtonRole.AcceptRole)
    box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(confirm_button)
    box.exec()
    return box.clickedButton() is confirm_button
