"""Header notification bell. Deliberately simple: it's a visible anchor for
"where would notifications live" rather than a full inbox — the actual
feedback mechanism today is the toast NotificationCenter (see toast.py) for
transient success/error messages. Clicking the bell shows the most recent
ones as a popup list rather than nothing happening.
"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLabel, QMenu, QToolButton


class NotificationBell(QToolButton):
    clicked_bell = Signal()

    def __init__(self):
        super().__init__()
        self.setText("🔔")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setObjectName("notificationBell")
        self._history: list[tuple[str, str]] = []  # (kind, message)
        self._badge = QLabel(self)
        self._badge.setFixedSize(8, 8)
        self._badge.setObjectName("notificationBadge")
        self._badge.move(20, 4)
        self._badge.hide()
        self.clicked.connect(self._show_history)

    def record(self, kind: str, message: str) -> None:
        self._history.insert(0, (kind, message))
        self._history = self._history[:8]
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
