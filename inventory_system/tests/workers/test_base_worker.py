"""Worker/WorkerSignals lifetime.

Two properties, tested separately:

1. GC-survival: without an explicit strong reference, Python's GC could
   collect a Worker (and its .signals QObject) while it was still running
   on a QThreadPool thread, crashing with "RuntimeError: Signal source has
   been deleted" the moment it tried to emit. _in_flight (see
   app/workers/base_worker.py) fixes this by holding a reference for the
   worker's lifetime regardless of what the caller does with its local
   variable. Tested synchronously — no event loop needed.

2. Eventual release: _release() now defers the actual _in_flight.discard()
   via QTimer.singleShot(0, ...) rather than doing it immediately (see
   tests/workers/test_worker_multi_slot_delivery.py for why: discarding
   immediately, while _release is being invoked as one of *several* slots
   connected to the same signal, was found to silently stop the other
   slot(s) from ever running — the actual cause of a real "Products page
   stuck loading forever" bug in the running app). Because the discard is
   now deferred, verifying it needs a moment of real event-loop processing
   after calling _release() — QCoreApplication.processEvents() in a short
   loop, not a full app.exec().
"""
import gc

import pytest

try:
    from PySide6.QtCore import QCoreApplication
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


def _pump_events(iterations=50):
    for _ in range(iterations):
        QCoreApplication.processEvents()


def test_worker_is_kept_alive_by_the_registry_after_caller_drops_its_reference(qapp):
    from PySide6.QtCore import QThreadPool

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


def test_release_eventually_removes_a_worker_from_the_registry(qapp):
    # Checks this specific worker's own membership, not a global count:
    # other tests' workers may still be mid-(deferred-)cleanup when this
    # runs, and _pump_events() below would incidentally process those too
    # — a global count comparison would be fragile against that unrelated
    # interleaving, even though this test's own behavior is correct.
    worker = Worker(lambda: None)  # __init__ adds itself to _in_flight
    assert worker in _in_flight

    worker._release()
    assert worker in _in_flight, (
        "release should be deferred, not immediate — see module docstring")

    _pump_events()

    assert worker not in _in_flight


def test_multiple_in_flight_workers_are_each_tracked_independently(qapp):
    workers = [Worker(lambda: None) for _ in range(3)]
    gc.collect()
    assert all(w in _in_flight for w in workers)

    for w in workers:
        w._release()
    _pump_events()

    assert all(w not in _in_flight for w in workers)
