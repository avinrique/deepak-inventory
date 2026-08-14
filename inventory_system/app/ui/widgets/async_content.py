"""Generic loading -> content|empty|error swapper for pages that fetch data
via a Service on a background thread (app.workers.base_worker.Worker, on
the shared QThreadPool) — keeps that orchestration in one place instead of
Dashboard/Inventory/Sales/Purchases each reimplementing the same dance, and
keeps I/O off the UI thread.
"""
import logging
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QStackedWidget, QWidget

from app.ui.widgets.states import ErrorStateWidget, LoadingStateWidget
from app.workers.base_worker import Worker

_logger = logging.getLogger(__name__)


class AsyncContentArea(QStackedWidget):
    def __init__(self, load: Callable[[], Any], render: Callable[[Any], QWidget],
                is_empty: Callable[[Any], bool], empty_state: QWidget,
                error_message: str = "Couldn't load this data."):
        super().__init__()
        self._load = load
        self._render = render
        self._is_empty = is_empty
        self._content_widget: QWidget | None = None
        # Bumped on every reload(); a completion is only applied if it's
        # still the most recent one requested. Without this, two
        # overlapping reload() calls (a real scenario — e.g. archiving a
        # row re-triggers a reload while a slightly earlier one, such as
        # from clearing a search box, hasn't finished yet) can complete out
        # of order: an older, slower request finishing *after* a newer one
        # would silently overwrite fresh data with stale data.
        self._generation = 0

        self._loading_widget = LoadingStateWidget()
        self._empty_widget = empty_state
        self._error_widget = ErrorStateWidget(error_message, on_retry=self.reload)
        self.addWidget(self._loading_widget)
        self.addWidget(self._empty_widget)
        self.addWidget(self._error_widget)

        self.reload()

    def reload(self) -> None:
        self._generation += 1
        generation = self._generation
        self.setCurrentWidget(self._loading_widget)
        worker = Worker(self._load)
        worker.signals.finished.connect(lambda result: self._on_loaded(result, generation))
        worker.signals.error.connect(lambda exc: self._on_error(exc, generation))
        QThreadPool.globalInstance().start(worker)

    def _on_loaded(self, result: Any, generation: int) -> None:
        if generation != self._generation:
            return  # superseded by a newer reload() — this result is stale
        if self._is_empty(result):
            self.setCurrentWidget(self._empty_widget)
            return
        if self._content_widget is not None:
            self.removeWidget(self._content_widget)
            self._content_widget.deleteLater()
        self._content_widget = self._render(result)
        self.addWidget(self._content_widget)
        self.setCurrentWidget(self._content_widget)

    def _on_error(self, exc: Exception, generation: int) -> None:
        if generation != self._generation:
            return
        _logger.exception("Loading data failed", exc_info=exc)
        self.setCurrentWidget(self._error_widget)
