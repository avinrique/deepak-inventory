"""AsyncContentArea's out-of-order-response protection — regression test
for a real race found during UI verification: archiving a product
triggered a reload() while an earlier, slower reload() (e.g. from clearing
a search box moments before) hadn't finished yet; the earlier one finished
*later* and silently overwrote the fresh post-archive table with stale
pre-archive data. Fixed by tagging each reload() with a generation number
and ignoring completions that aren't from the most recent one.

Exercises _on_loaded/_on_error directly with explicit generation numbers
rather than via real QThreadPool timing — this environment's QTest.qWait
does not reliably pump cross-thread QThreadPool signal delivery (confirmed
separately during UI verification: a call that completes in ~1.4s under a
real app.exec() loop never completed under qWait-based polling), so a
timing-dependent version of this test would be flaky for reasons unrelated
to the thing being tested. The generation-comparison logic itself doesn't
need real threads to verify.
"""
import pytest

try:
    from PySide6.QtWidgets import QApplication, QLabel
except ImportError:
    pytest.skip("PySide6 not available", allow_module_level=True)

from app.ui.widgets.async_content import AsyncContentArea


@pytest.fixture(scope="module")
def qapp():
    try:
        app = QApplication.instance() or QApplication([])
    except Exception as exc:  # noqa: BLE001 - e.g. no display available
        pytest.skip(f"cannot create QApplication: {exc}")
    return app


def _area(qapp):
    return AsyncContentArea(load=lambda: None, render=lambda tag: QLabel(tag),
                            is_empty=lambda r: False, empty_state=QLabel("empty"))


def test_reload_increments_generation_each_call(qapp):
    area = _area(qapp)
    first = area._generation
    area.reload()
    assert area._generation == first + 1
    area.reload()
    assert area._generation == first + 2


def test_on_loaded_from_current_generation_is_applied(qapp):
    area = _area(qapp)
    area.reload()
    current = area._generation

    area._on_loaded("fresh", current)

    assert area._content_widget.text() == "fresh"
    assert area.currentWidget() is area._content_widget


def test_on_loaded_from_a_stale_generation_is_ignored(qapp):
    area = _area(qapp)
    area.reload()
    stale_generation = area._generation
    area._on_loaded("fresh", stale_generation)  # apply an initial result
    assert area._content_widget.text() == "fresh"

    area.reload()  # a newer request starts — generation moves on
    # The stale request now finishes late and tries to apply its result.
    area._on_loaded("stale and wrong", stale_generation)

    assert area._content_widget.text() == "fresh", (
        "a late result from a superseded reload() overwrote the current content")


def test_late_result_from_the_newest_generation_still_applies(qapp):
    area = _area(qapp)
    area.reload()
    gen_a = area._generation
    area.reload()
    gen_b = area._generation
    assert gen_b != gen_a

    # gen_a (older) finishes first with stale data — ignored.
    area._on_loaded("from A", gen_a)
    assert area._content_widget is None or area._content_widget.text() != "from A"

    # gen_b (the one actually requested most recently) finishes — applied.
    area._on_loaded("from B", gen_b)
    assert area._content_widget.text() == "from B"


def test_on_error_from_a_stale_generation_is_ignored(qapp):
    area = _area(qapp)
    area.reload()
    current = area._generation
    area._on_loaded("fresh", current)

    area.reload()  # supersede it
    area._on_error(RuntimeError("stale failure"), current)

    assert area.currentWidget() is not area._error_widget, (
        "a stale error from a superseded reload() incorrectly showed the error state")


def test_on_error_from_the_current_generation_shows_error_state(qapp):
    area = _area(qapp)
    area.reload()
    current = area._generation

    area._on_error(RuntimeError("boom"), current)

    assert area.currentWidget() is area._error_widget
