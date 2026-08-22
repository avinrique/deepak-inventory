"""
Test isolation for the Inventory Management app.

Three jobs, all safety-critical:

1. Point ``storage`` at a throwaway folder for every test.
2. Disable the legacy-data migration, so the sandbox really is empty. Without
   this, ``data_dir()`` copies ``<repo>/inventory_data/*.xlsx`` into every
   tmp_path and the whole suite runs against the developer's sample books.
3. Refuse to run if the sandbox is ever escaped. These tests write, void and
   rebuild workbooks; a stray real path would destroy a shop's books. The guard
   runs before AND after each test, so a test that resets the environment
   mid-way (``monkeypatch.undo()`` is the classic one) fails loudly instead of
   quietly operating on live data.
"""

import os

import pytest

import storage


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    """Give each test its own genuinely empty data folder."""
    sandbox = tmp_path / "data"
    sandbox.mkdir()
    monkeypatch.setenv("INVENTORY_DATA_DIR", str(sandbox))
    # Point the legacy location at somewhere that does not exist, so the
    # one-time migration is a no-op instead of seeding the sandbox.
    monkeypatch.setattr(storage, "_legacy_data_dir",
                        lambda: str(tmp_path / "no-legacy"))
    storage.reset_data_dir()

    resolved = _assert_sandboxed(sandbox, "at test setup")
    assert os.listdir(resolved) == [], (
        f"the sandbox is not empty at test setup: {os.listdir(resolved)}")

    # Preventive, not post-mortem: every data_dir() call is checked, so a test
    # that escapes the sandbox fails on the FIRST resolution rather than after
    # its writes have already landed somewhere real.
    real_data_dir = storage.data_dir

    def guarded_data_dir():
        path = real_data_dir()
        expected = os.path.realpath(str(sandbox))
        if os.path.commonpath([os.path.realpath(path), expected]) != expected:
            pytest.fail(
                f"REFUSING TO CONTINUE: storage.data_dir() resolved to {path}, "
                f"outside the test sandbox {expected}. A write would have hit "
                f"real data.")
        return path

    monkeypatch.setattr(storage, "data_dir", guarded_data_dir)
    try:
        yield storage
    finally:
        monkeypatch.setattr(storage, "data_dir", real_data_dir)
        storage.release_single_instance_lock()
        _assert_sandboxed(sandbox, "at test teardown")
        storage.reset_data_dir()


def _assert_sandboxed(sandbox, when: str) -> str:
    resolved = os.path.realpath(storage.data_dir())
    expected = os.path.realpath(str(sandbox))
    if os.path.commonpath([resolved, expected]) != expected:
        pytest.fail(
            f"REFUSING TO RUN {when}: storage resolved to {resolved}, which is "
            f"outside the test sandbox {expected}. A test would have written "
            f"to real data. Check that nothing reset INVENTORY_DATA_DIR.")
    return resolved
