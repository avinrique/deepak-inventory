"""Worker/WorkerSignals lifetime — regression test for a real bug found
during UI verification: without an explicit strong reference, Python's GC
could collect a Worker (and its .signals QObject) while it was still
running on a QThreadPool thread, crashing with "RuntimeError: Signal
source has been deleted" the moment it tried to emit. _in_flight (see
app/workers/base_worker.py) fixes this by holding a reference for the
worker's lifetime regardless of what the caller does with its local
variable.

This tests that GC-survival property directly and synchronously — it does
not need the background thread to actually run to completion, which is
deliberate: this sandboxed environment's QThreadPool cross-thread signal
delivery has proven unreliable to test against directly (both
QTest.qWait-based polling and even nested app.exec() calls across several
tests in the same process were flaky here), while a single, fresh,
standalone process reliably completes the same call in ~1.4s — see
docs/architecture.md and the session's UI verification notes. That's a
property of this test environment, not of the Worker class, so this test
is scoped to what it can prove deterministically: an unreferenced,
in-flight Worker is not garbage collected.
"""
import gc

import pytest

try:
    from PySide6.QtCore import QThreadPool
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


def test_worker_is_kept_alive_by_the_registry_after_caller_drops_its_reference(qapp):
    before = len(_in_flight)

    def start_and_forget():
        worker = Worker(lambda: None)
        QThreadPool.globalInstance().start(worker)
        # No reference retained beyond this function — before the fix,
        # this was the exact scenario that let GC collect the Worker (and
        # its .signals QObject) while still running on a QThreadPool
        # thread.

    start_and_forget()
    gc.collect()

    assert len(_in_flight) == before + 1, "the in-flight Worker was garbage collected"


def test_release_removes_a_worker_from_the_registry(qapp):
    before = len(_in_flight)
    worker = Worker(lambda: None)  # __init__ adds itself to _in_flight
    assert len(_in_flight) == before + 1

    worker._release()

    assert len(_in_flight) == before


def test_multiple_in_flight_workers_are_each_tracked_independently(qapp):
    before = len(_in_flight)
    workers = [Worker(lambda: None) for _ in range(3)]
    for w in workers:
        QThreadPool.globalInstance().start(w)
    gc.collect()

    assert len(_in_flight) == before + 3

    for w in workers:
        w._release()
    assert len(_in_flight) == before
