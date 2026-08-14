"""Toast notifications — success/error/info messages that appear over the
content area and auto-dismiss, for feedback that doesn't warrant a modal
dialog (login errors, logout confirmation, save results once pages write
data).
"""
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QGraphicsOpacityEffect, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from app.ui.theme import GREEN, RED, TEXT

_KIND_STYLE = {
    "success": (GREEN, "✓"),
    "error": (RED, "✕"),
    "info": ("#2563eb", "ⓘ"),
}


class Toast(QWidget):
    def __init__(self, parent: QWidget, message: str, kind: str = "info",
                duration_ms: int = 3500):
        super().__init__(parent)
        color, icon = _KIND_STYLE.get(kind, _KIND_STYLE["info"])
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            QWidget {{
                background: {TEXT};
                border-radius: 8px;
            }}
            QLabel {{ color: white; background: transparent; }}
        """)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 10, 16, 10)
        layout.setSpacing(10)
        badge = QLabel(icon)
        badge.setStyleSheet(f"color: {color}; font-weight: 700; font-size: 14px;")
        layout.addWidget(badge)
        text = QLabel(message)
        text.setWordWrap(True)
        layout.addWidget(text, stretch=1)

        self._opacity = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._opacity)
        self._opacity.setOpacity(1.0)
        QTimer.singleShot(duration_ms, self._fade_out)

    def _fade_out(self):
        self.deleteLater()


class NotificationCenter(QWidget):
    """Anchored to the bottom-right of ``host``; stacks active toasts."""

    def __init__(self, host: QWidget):
        super().__init__(host)
        self._host = host
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 20, 20)
        self._layout.setSpacing(8)
        self._layout.setAlignment(Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignRight)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        host.installEventFilter(self)
        self._reposition()
        self.raise_()

    def eventFilter(self, watched, event):
        if watched is self._host and event.type() == event.Type.Resize:
            self._reposition()
        return False

    def _reposition(self):
        self.setGeometry(0, 0, self._host.width(), self._host.height())

    def _show(self, message: str, kind: str):
        toast = Toast(self, message, kind)
        toast.setMaximumWidth(360)
        self._layout.addWidget(toast)
        toast.show()
        self.raise_()

    def success(self, message: str):
        self._show(message, "success")

    def error(self, message: str):
        self._show(message, "error")

    def info(self, message: str):
        self._show(message, "info")
