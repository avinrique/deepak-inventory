"""Initials avatar — the colored circle with a person's initials used
wherever "who is this" needs a visual: the header's UserMenu, the Users
admin table, and the current user's own Profile page. One place for the
initials-from-a-name rule and its styling, instead of one copy per caller
that could quietly drift (circle size and font size are the only knobs
that should vary by context).
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel

from app.ui.theme import ACCENT, scale


def initials(full_name: str) -> str:
    parts = [p for p in full_name.strip().split() if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


class AvatarLabel(QLabel):
    def __init__(self, full_name: str, size: int = 34, font_size: int = 12):
        super().__init__(initials(full_name))
        self.setFixedSize(scale(size), scale(size))
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet(f"""
            background: {ACCENT}; color: white; border-radius: {size // 2}px;
            font-size: {font_size}px; font-weight: 700;
        """)
