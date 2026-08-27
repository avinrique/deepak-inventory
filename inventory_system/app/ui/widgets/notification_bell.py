"""Header notification bell. Deliberately simple: it's a visible anchor for
"where would notifications live" rather than a full inbox — the actual
feedback mechanism today is the toast NotificationCenter (see toast.py) for
transient success/error messages. Clicking the bell shows the most recent
ones as a popup list rather than nothing happening.
"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLabel, QMenu, QToolButton
from app.ui.theme import scale


class NotificationBell(QToolButton):
    clicked_bell = Signal()

    def __init__(self):
        super().__init__()
        self.setText("🔔")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setObjectName("notificationBell")
        self._history: list[tuple[str, str]] = []  # (kind, message)
        self._badge = QLabel(self)
        self._badge.setFixedSize(scale(8), scale(8))
        self._badge.setObjectName("notificationBadge")
        self._badge.hide()
        self._position_badge()
        self.clicked.connect(self._show_history)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        """Pins the unread dot to the button's top-right corner.

        It used to sit at a hardcoded move(20, 4), which was measured against
        one particular font at one particular scale factor. At any other
        size the dot drifted off the bell glyph -- and on Windows, where the
        emoji comes from a different font than on macOS, it drifted anyway.
        """
        super().resizeEvent(event)
        self._position_badge()

    def _position_badge(self) -> None:
        inset = scale(3)
        self._badge.move(max(0, self.width() - self._badge.width() - inset), inset)

    def record(self, kind: str, message: str) -> None:
        self._history.insert(0, (kind, message))
        self._history = self._history[:8]
        self._position_badge()
        self._badge.show()

    def _show_history(self) -> None:
        self._badge.hide()
        menu = QMenu(self)
        if not self._history:
            action = menu.addAction("No notifications yet")
            action.setEnabled(False)
        else:
            icons = {"success": "✓", "error": "✕", "info": "ⓘ"}
            for kind, message in self._history:
                menu.addAction(f"{icons.get(kind, 'ⓘ')}  {message}").setEnabled(False)
        menu.exec(self.mapToGlobal(self.rect().bottomLeft()))
