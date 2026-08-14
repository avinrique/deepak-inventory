"""Regression test for a real, serious bug: Worker.__init__ connects its
own cleanup slot (_release, which drops the last reference keeping the
Worker's .signals QObject alive) to the same finished/error signal a
caller also connects to (e.g. AsyncContentArea._on_loaded). Because
_release is connected *first*, it used to run first and release the
worker while Qt was still delivering that same emit() to the caller's
slot — which silently stopped the caller's slot from ever being invoked.

This is what made the Products page (and, transiently, other pages) hang
on "Loading..." forever in the real running app: the backend call
completed and emit() was called, but the page's own _on_loaded callback
never ran. Fixed by deferring _release's actual cleanup via
QTimer.singleShot(0, ...) so every slot connected to the same emit() gets
its turn first — this test proves that ordering directly rather than via
a full app.exec() UI flow, which is slow and was how the bug was
originally found.
"""
import gc

import pytest

try:
    from PySide6.QtCore import QThreadPool, QTimer
    from PySide6.QtWidgets import QApplication
except ImportError:
    pytest.skip("PySide6 not available", allow_module_level=True)

from app.workers.base_worker import Worker, _in_flight


@pytest.fixture(scope="module")
def qapp():
    try:
        app = QApplication.instance() or QApplication([])
    except Exception as exc:  # noqa: BLE001 - e.g. no display available
        pytest.skip(f"cannot create QApplication: {exc}")
    return app


def _run_in_event_loop(qapp, start_fn, timeout_ms=10000):
    QTimer.singleShot(0, start_fn)
    watchdog = QTimer()
    watchdog.setSingleShot(True)
    watchdog.timeout.connect(lambda: qapp.exit(2))
    watchdog.start(timeout_ms)
    code = qapp.exec()
    watchdog.stop()
    if code == 2:
        pytest.fail(f"timed out after {timeout_ms}ms — the second slot never fired")
    return code


def test_a_second_slot_connected_to_finished_still_receives_it(qapp):
    """The exact shape AsyncContentArea uses: Worker's own _release plus a
    second, caller-provided slot both connected to `finished`. Both must
    run — not just the first one connected.
    """
    received = {}

    def start():
        worker = Worker(lambda: "the result")

        def caller_slot(result):
            received["result"] = result
            qapp.exit(0)

        worker.signals.finished.connect(caller_slot)  # connected *after* _release
        QThreadPool.globalInstance().start(worker)
        gc.collect()  # no local `worker` reference survives this function

    _run_in_event_loop(qapp, start)

    assert received.get("result") == "the result"


def test_a_second_slot_connected_to_error_still_receives_it(qapp):
    received = {}

    def boom():
        raise ValueError("boom")

    def start():
        worker = Worker(boom)

        def caller_slot(exc):
            received["exc"] = exc
            qapp.exit(0)

        worker.signals.error.connect(caller_slot)
        QThreadPool.globalInstance().start(worker)
        gc.collect()

    _run_in_event_loop(qapp, start)

    assert isinstance(received.get("exc"), ValueError)


def test_worker_is_eventually_released_even_with_a_second_slot(qapp):
    # Checks membership of *this* worker specifically, rather than a global
    # _in_flight count — other tests' own workers may still be mid-cleanup
    # (their release is deferred too), so an exact count comparison here
    # would be fragile against that unrelated, unpredictable interleaving.
    holder = {}
    done = {}

    def start():
        worker = Worker(lambda: None)
        holder["worker"] = worker
        worker.signals.finished.connect(lambda _v: done.update(ok=True))
        QThreadPool.globalInstance().start(worker)

    def check_released():
        if done.get("ok") and holder["worker"] not in _in_flight:
            qapp.exit(0)
        else:
            QTimer.singleShot(50, check_released)

    QTimer.singleShot(0, start)
    QTimer.singleShot(100, check_released)
    watchdog = QTimer()
    watchdog.setSingleShot(True)
    watchdog.timeout.connect(lambda: qapp.exit(2))
    watchdog.start(10000)
    code = qapp.exec()
    watchdog.stop()

    assert code == 0, "worker was never released from the registry"
    assert done.get("ok") is True
    assert holder["worker"] not in _in_flight
