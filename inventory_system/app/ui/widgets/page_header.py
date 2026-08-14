"""Consistent title+subtitle block reused at the top of every page — the
single place page typography/spacing is defined, rather than 11 pages each
rebuilding it slightly differently.
"""
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget


class PageHeader(QWidget):
    def __init__(self, title: str, subtitle: str = ""):
        super().__init__()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 24, 28, 6)
        outer.setSpacing(2)

        title_label = QLabel(title)
        title_label.setObjectName("pageTitle")
        outer.addWidget(title_label)

        if subtitle:
            subtitle_label = QLabel(subtitle)
            subtitle_label.setObjectName("pageSubtitle")
            outer.addWidget(subtitle_label)
