"""Reusable loading/empty/error placeholders — every page that fetches data
(Dashboard, Inventory, Sales, Purchases) uses these instead of inventing its
own "no data" label, so the pattern reads the same everywhere.
"""
from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QProgressBar, QPushButton, QVBoxLayout, QWidget

from app.ui.theme import MUTED, TEXT, scale


class _CenteredState(QWidget):
    def __init__(self, icon: str, title: str, message: str = "",
                action_label: str | None = None, on_action: Callable[[], None] | None = None):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(6)

        icon_label = QLabel(icon)
        icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_label.setStyleSheet("font-size: 40px;")
        layout.addWidget(icon_label)

        title_label = QLabel(title)
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_label.setStyleSheet(f"font-size: 15px; font-weight: 700; color: {TEXT};")
        layout.addWidget(title_label)

        # Always constructed, even when empty: an ErrorStateWidget is created
        # once with a generic message and then re-labelled with the real
        # cause when a load actually fails (see set_message).
        self._message_label = QLabel(message)
        self._message_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._message_label.setWordWrap(True)
        self._message_label.setStyleSheet(f"font-size: 12px; color: {MUTED};")
        self._message_label.setMaximumWidth(scale(360))
        self._message_label.setVisible(bool(message))
        layout.addWidget(self._message_label, alignment=Qt.AlignmentFlag.AlignHCenter)

        if action_label and on_action is not None:
            layout.addSpacing(8)
            button = QPushButton(action_label)
            button.setObjectName("primary")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(on_action)
            layout.addWidget(button, alignment=Qt.AlignmentFlag.AlignHCenter)


    def set_state_message(self, message: str) -> None:
        self._message_label.setText(message)
        self._message_label.setVisible(bool(message))


class LoadingStateWidget(_CenteredState):
    def __init__(self, message: str = "Loading…"):
        super().__init__(icon="", title="", message="")
        # Replace the icon slot with a real indeterminate progress bar
        # rather than a static glyph — this is the one state that's
        # actively in progress, so it should visibly move.
        layout = self.layout()
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        bar = QProgressBar()
        bar.setRange(0, 0)  # indeterminate
        bar.setFixedSize(scale(160), scale(6))
        bar.setTextVisible(False)
        layout.addWidget(bar, alignment=Qt.AlignmentFlag.AlignHCenter)
        label = QLabel(message)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setStyleSheet(f"font-size: 12px; color: {MUTED}; margin-top: 8px;")
        layout.addWidget(label)


class EmptyStateWidget(_CenteredState):
    def __init__(self, title: str, message: str = "", icon: str = "🗂️",
                action_label: str | None = None, on_action: Callable[[], None] | None = None):
        super().__init__(icon=icon, title=title, message=message,
                         action_label=action_label, on_action=on_action)


class ErrorStateWidget(_CenteredState):
    def __init__(self, message: str = "Something went wrong.",
                on_retry: Callable[[], None] | None = None):
        self._default_message = message
        super().__init__(icon="⚠️", title="Couldn't load this", message=message,
                         action_label="Retry" if on_retry else None, on_action=on_retry)

    def set_message(self, message: str | None) -> None:
        """Replaces the placeholder with what actually went wrong.

        "Couldn't load this data." is true of a dropped network connection,
        a wrong password and an expired session alike, and tells the user
        which of those to go and fix: none of them.
        """
        self.set_state_message(message or self._default_message)
