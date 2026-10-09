"""packaging/fetch_pgserver.py — the staging script's completeness check.

Worth testing rather than trusting, because the thing it guards against is
invisible in every obvious experiment. PostgreSQL falls back to the share
directory of a system installation when the one next to the binaries is
incomplete, so a bundle missing half of share/ initialises a database
perfectly on any machine that has PostgreSQL installed — every developer's,
and the CI runner's — and fails on the first customer PC that does not.

That is not hypothetical: share/tsearch_data was pruned during development,
passed a real end-to-end initdb on macOS, and would have shipped a build
whose "keep my data on this computer" option failed at first run.
"""
import importlib.util
from pathlib import Path

import pytest

_SCRIPT = (Path(__file__).resolve().parents[2] / "packaging" / "fetch_pgserver.py")


@pytest.fixture(scope="module")
def script():
    """Loaded by path: packaging/ is not a package and must not be put on
    sys.path just for this."""
    spec = importlib.util.spec_from_file_location("fetch_pgserver", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _complete_bundle(root: Path, *, languages=("english", "french")) -> Path:
    """The minimum a staged tree needs to look usable."""
    binaries = root / "bin"
    binaries.mkdir(parents=True)
    for name in ("initdb", "pg_ctl", "postgres"):
        (binaries / name).write_text("")
        (binaries / f"{name}.exe").write_text("")

    share = root / "share"
    share.mkdir()
    for name in ("postgres.bki", "system_functions.sql", "system_views.sql",
                 "information_schema.sql", "postgresql.conf.sample",
                 "pg_hba.conf.sample"):
        (share / name).write_text("")
    (share / "timezone").mkdir()
    (share / "tsearch_data").mkdir()

    clauses = "\n".join(
        f"CREATE TEXT SEARCH DICTIONARY {language}_stem "
        f"(TEMPLATE = snowball, Language = {language} , StopWords={language});"
        for language in languages)
    (share / "snowball_create.sql").write_text(clauses)
    for language in languages:
        (share / "tsearch_data" / f"{language}.stop").write_text("")
    return root


def test_a_complete_bundle_passes(script, tmp_path):
    assert script._verify_staged_share(_complete_bundle(tmp_path)) == []


def test_missing_stop_words_are_caught(script, tmp_path):
    """The exact bug this check exists for. initdb runs snowball_create.sql
    while bootstrapping, and creating each dictionary reads its stop-word
    file — a missing one is an ERROR, not a warning."""
    root = _complete_bundle(tmp_path)
    (root / "share" / "tsearch_data" / "french.stop").unlink()

    problems = script._verify_staged_share(root)

    assert len(problems) == 1
    assert "french" in problems[0]
    assert "english" not in problems[0], "it blamed a language that was present"


def test_a_missing_bootstrap_catalog_is_caught(script, tmp_path):
    root = _complete_bundle(tmp_path)
    (root / "share" / "postgres.bki").unlink()

    problems = script._verify_staged_share(root)

    assert any("postgres.bki" in problem for problem in problems)


def test_a_missing_timezone_database_is_caught(script, tmp_path):
    """Every timestamptz column depends on it, and the schema is full of
    them — but initdb succeeds without it, so nothing else would notice."""
    root = _complete_bundle(tmp_path)
    (root / "share" / "timezone").rmdir()

    problems = script._verify_staged_share(root)

    assert any("timezone" in problem for problem in problems)


def test_a_missing_binary_is_caught(script, tmp_path):
    root = _complete_bundle(tmp_path)
    for suffix in ("", ".exe"):
        (root / "bin" / f"pg_ctl{suffix}").unlink()

    problems = script._verify_staged_share(root)

    assert any("pg_ctl" in problem for problem in problems)


def test_the_nested_share_layout_is_understood(script, tmp_path):
    """Layouts differ: Windows and EnterpriseDB builds put the bootstrap
    files straight in share/, Homebrew nests them in share/postgresql@16/.
    Assuming either one would make this check pass vacuously on the other."""
    root = tmp_path / "nested"
    inner = _complete_bundle(root / "staged")
    # Re-shape into the nested form: share/<versioned dir>/...
    (root / "bin").mkdir(parents=True, exist_ok=True)
    for binary in (inner / "bin").iterdir():
        (root / "bin" / binary.name).write_text("")
    nested_share = root / "share" / "postgresql@16"
    nested_share.parent.mkdir(parents=True, exist_ok=True)
    (inner / "share").rename(nested_share)

    assert script._verify_staged_share(root) == []


def test_an_empty_share_is_not_mistaken_for_a_complete_one(script, tmp_path):
    """The failure mode of a path-based check: finding nothing and reporting
    nothing wrong."""
    root = tmp_path / "hollow"
    (root / "bin").mkdir(parents=True)
    for name in ("initdb", "pg_ctl", "postgres"):
        (root / "bin" / name).write_text("")
        (root / "bin" / f"{name}.exe").write_text("")
    (root / "share").mkdir()

    problems = script._verify_staged_share(root)

    assert problems, "an empty share/ was reported as usable"
