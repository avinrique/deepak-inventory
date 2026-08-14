"""Generic QRunnable worker: runs ``fn`` on a background thread pool and
reports back via Qt signals instead of a return value, so a page can call a
Service method without blocking the UI thread. Errors are emitted, not
swallowed.

Usage:

    worker = Worker(billing_service.create_sale, bill)
    worker.signals.finished.connect(on_saved)
    worker.signals.error.connect(on_error)
    QThreadPool.globalInstance().start(worker)
"""
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QRunnable, Signal, Slot

# Keeps a strong Python reference to every in-flight Worker so it (and its
# .signals QObject) can't be garbage-collected while still running on a
# QThreadPool thread. Once .start(worker) returns, the caller's local
# `worker` variable is typically the only Python reference — as soon as it
# goes out of scope, GC is free to collect it before the background thread
# finishes and calls self.signals.<...>.emit(), which then fails with
# "RuntimeError: Signal source has been deleted". Real, not hypothetical:
# this fired during UI verification, not under any unusual load.
_in_flight: set["Worker"] = set()


class WorkerSignals(QObject):
    finished = Signal(object)
    error = Signal(Exception)


class Worker(QRunnable):
    def __init__(self, fn: Callable[..., Any], *args: Any, **kwargs: Any):
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.signals = WorkerSignals()
        self.signals.finished.connect(self._release)
        self.signals.error.connect(self._release)
        _in_flight.add(self)

    def _release(self, *_ignored: Any) -> None:
        _in_flight.discard(self)

    @Slot()
    def run(self) -> None:
        try:
            result = self.fn(*self.args, **self.kwargs)
        except Exception as exc:  # noqa: BLE001 - surfaced via signal, not swallowed
            self.signals.error.emit(exc)
        else:
            self.signals.finished.emit(result)
