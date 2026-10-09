#!/usr/bin/env python3
"""Stages a whole PostgreSQL server into packaging/pgsql/ for bundling.

This is what makes "keep the database on this computer" possible. A shop
that just ran the installer has no PostgreSQL and no reason to administer
one, so the installer carries a server with it and
app.database.local_server runs it. Without this script the application
still builds and runs -- the setup wizard disables its local option and
explains why -- so a build box with no PostgreSQL is not a hard failure.

    python packaging/fetch_pgserver.py                      # find one automatically
    python packaging/fetch_pgserver.py --source "C:\\Program Files\\PostgreSQL\\16"
    python packaging/fetch_pgserver.py --require-major 16    # refuse anything else
    python packaging/fetch_pgserver.py --optional            # exit 0 if none found

--source takes the *installation root* (the directory holding bin/, lib/
and share/), not the bin/ directory that fetch_pgtools.py wants.

**--require-major is not optional in CI.** PostgreSQL will not open a data
directory written by a different major version -- it enforces that itself,
and the on-disk format really does change. CI copies from whatever
PostgreSQL the runner image happens to have, so without a pin, a runner
image update silently bumps the bundled major and the next release refuses
to start against every existing customer's data. Pin it, and fail the build
rather than ship the mismatch.

This supersedes fetch_pgtools.py for a normal build: pg_dump and pg_restore
live in the same bin/ as initdb and pg_ctl, so staging them separately
would ship the same binaries twice. app.core.paths.pg_bin_dir() prefers
pgsql/bin for exactly that reason.

Licensing: PostgreSQL is distributed under the PostgreSQL License, which is
permissive and allows redistribution provided the copyright notice travels
with it. The notice is copied from the installation when one is present.
"""
import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

DESTINATION = Path(__file__).resolve().parent / "pgsql"

VERSION_MARKER = "PGSQL_VERSION.txt"

# What the application actually invokes. pg_dump/pg_restore are not in this
# list because Backup degrades gracefully without them, whereas a missing
# initdb means the whole local-database feature is dead.
REQUIRED = ["initdb", "pg_ctl", "postgres"]

# Copied when present, not required: Backup and Restore use these.
WANTED = ["pg_dump", "pg_restore", "psql"]

# Pruned from share/ -- documentation only, and deliberately nothing else.
#
# The temptation is to trim this much harder, and it is a trap. share/ is
# what initdb reads to build a cluster: postgres.bki bootstraps the system
# catalogs, the *.sql files define the catalog views and functions,
# timezone/ backs every timestamptz column, and tsearch_data/*.stop is read
# while snowball_create.sql creates the fifteen default text-search
# dictionaries. Dropping any of it makes initdb fail *on the customer's
# machine*, long after the build looked fine.
#
# That failure also hides during development: PostgreSQL falls back to the
# share directory of a system installation, so a developer or CI runner with
# PostgreSQL installed sees initdb succeed against files that are not in the
# bundle at all. tsearch_data was pruned here at first for exactly that
# reason -- it tested clean on macOS and would have shipped broken. Hence
# _verify_staged_share() below, which checks the bundle rather than the
# machine.
#
# locale/ is the one large exception, and it is safe for a different reason
# than "nothing reads it": it holds gettext catalogs that translate
# PostgreSQL's *own* messages, and gettext falls back to the original
# English when a catalog is absent. Nothing in the bootstrap reads it, and
# the cluster is initialised with --locale=C regardless. It is also around
# 20 MB -- most of share/ -- so dropping it is the only pruning here that
# actually pays for itself.
SHARE_PRUNE = {"doc", "man", "locale"}

# Debug symbols and import libraries: build-time artefacts with no runtime
# use, and together a large share of the payload.
PRUNE_SUFFIXES = {".pdb", ".lib", ".exp", ".a", ".h"}


def _executable(directory: Path, name: str) -> Path:
    suffix = ".exe" if sys.platform == "win32" else ""
    return directory / f"{name}{suffix}"


def _candidate_roots() -> list[Path]:
    """Installation roots to try, newest major version first."""
    found: list[Path] = []
    for root in (Path(r"C:\Program Files\PostgreSQL"),
                 Path(r"C:\Program Files (x86)\PostgreSQL")):
        if not root.is_dir():
            continue
        for version_dir in sorted(root.iterdir(), reverse=True):
            if (version_dir / "bin").is_dir():
                found.append(version_dir)

    # Also honour whatever is on PATH, which covers Homebrew and apt for a
    # dry run on the machine this is developed on.
    #
    # resolve() first, and it matters: Homebrew's bin/initdb is a symlink
    # into the Cellar, so walking up from the link lands on /opt/homebrew
    # and "share" and "lib" there are every formula on the machine -- 450 MB
    # of unrelated files that copy without complaint.
    located = shutil.which("initdb")
    if located:
        found.append(Path(located).resolve().parent.parent)
    return found


def _is_usable(root: Path) -> bool:
    binary_dir = root / "bin"
    return (binary_dir.is_dir()
            and all(_executable(binary_dir, name).is_file() for name in REQUIRED)
            and (root / "share").is_dir())


def _major_version(root: Path) -> str | None:
    """Asked of the binary rather than parsed out of the path, because a
    directory can be called anything -- Homebrew's is "16" but a hand-built
    tree or an EnterpriseDB archive is just "pgsql"."""
    try:
        result = subprocess.run([str(_executable(root / "bin", "postgres")), "--version"],
                                capture_output=True, text=True, check=False, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"(\d+)\.", result.stdout) or re.search(r"(\d+)$", result.stdout.strip())
    return match.group(1) if match else None


def _copy_tree(source: Path, destination: Path, *, prune_names: set[str]) -> int:
    """Recursive copy that drops the pruned directory names and suffixes."""
    copied = 0
    destination.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        if item.name in prune_names:
            continue
        if item.is_dir():
            copied += _copy_tree(item, destination / item.name, prune_names=prune_names)
        elif item.suffix.lower() not in PRUNE_SUFFIXES:
            shutil.copy2(item, destination / item.name)
            copied += 1
    return copied


def _copy_bin(source: Path, destination: Path) -> int:
    """bin/ is filtered to the programs that are used, plus every DLL.

    A stock Windows bin/ holds around forty programs (clusterdb, vacuumlo,
    ecpg...) that nothing here invokes. The DLLs are copied wholesale rather
    than resolved from the import tables: guessing wrong produces an
    executable that fails to start behind an unhelpful system dialog, and
    the whole set is small next to share/.
    """
    destination.mkdir(parents=True, exist_ok=True)
    copied = 0
    for name in REQUIRED + WANTED:
        binary = _executable(source, name)
        if binary.is_file():
            shutil.copy2(binary, destination / binary.name)
            copied += 1
    for item in source.iterdir():
        if item.is_file() and item.suffix.lower() in {".dll", ".dylib", ".so"}:
            shutil.copy2(item, destination / item.name)
            copied += 1
    return copied


def _share_root(destination: Path) -> Path | None:
    """Where the bootstrap files actually landed.

    Layouts differ: a Windows/EnterpriseDB tree puts them directly in
    share/, while Homebrew nests them in share/postgresql@16/. Find
    postgres.bki rather than assuming either.
    """
    share = destination / "share"
    if (share / "postgres.bki").is_file():
        return share
    return next((found.parent for found in share.rglob("postgres.bki")), None)


def _verify_staged_share(destination: Path) -> list[str]:
    """Checks the staged bundle can bootstrap a cluster *on its own*.

    This exists because the obvious test does not work. Running initdb from
    the staged tree on a machine that has PostgreSQL installed can silently
    read the *system* share directory, so a bundle missing half of share/
    initialises a database perfectly well on the build machine and fails on
    the first customer PC that has no PostgreSQL. Checking for the files
    instead of exercising them is the only verification that means anything
    here.

    Returns a list of problems; empty means good.
    """
    problems: list[str] = []
    share = _share_root(destination)
    if share is None:
        return ["share/postgres.bki is missing — initdb cannot bootstrap a cluster"]

    # Read by initdb in order to create the system catalogs and views.
    for required in ("postgres.bki", "system_functions.sql", "system_views.sql",
                     "information_schema.sql", "snowball_create.sql",
                     "postgresql.conf.sample", "pg_hba.conf.sample"):
        if not (share / required).is_file():
            problems.append(f"share/{required} is missing")

    # Every timestamptz column depends on this.
    if not (share / "timezone").is_dir():
        problems.append("share/timezone/ is missing — timestamptz would not work")

    # snowball_create.sql creates one dictionary per language and each one
    # reads its stop-word file as it is created. A missing file is an ERROR
    # during initdb, not a warning.
    snowball = share / "snowball_create.sql"
    if snowball.is_file():
        languages = set(re.findall(r"StopWords\s*=\s*(\w+)",
                                   snowball.read_text(encoding="utf-8")))
        missing = sorted(language for language in languages
                         if not (share / "tsearch_data" / f"{language}.stop").is_file())
        if missing:
            problems.append(
                "share/tsearch_data is missing the stop-word files "
                f"snowball_create.sql requires ({', '.join(missing)}) — initdb "
                "would fail on a machine with no PostgreSQL of its own")

    for name in REQUIRED:
        if not _executable(destination / "bin", name).is_file():
            problems.append(f"bin/{name} is missing")

    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source",
                        help="PostgreSQL installation root (the directory "
                             "containing bin/, lib/ and share/).")
    parser.add_argument("--require-major", metavar="N",
                        help="Fail unless the PostgreSQL found is this major "
                             "version. Use this in CI -- see the module docstring.")
    parser.add_argument("--optional", action="store_true",
                        help="Exit 0 instead of 1 when no PostgreSQL is found.")
    args = parser.parse_args()

    candidates = [Path(args.source)] if args.source else _candidate_roots()
    source = next((root for root in candidates if _is_usable(root)), None)
    if source is None:
        message = ("Could not find a PostgreSQL server installation (needs bin/ with "
                   f"{', '.join(REQUIRED)} and a share/ directory). Install "
                   "PostgreSQL, or pass --source with its installation root.")
        if args.optional:
            print(f"Skipping: {message}")
            return 0
        print(message, file=sys.stderr)
        return 1

    major = _major_version(source)
    if args.require_major and major != args.require_major:
        print(f"Found PostgreSQL {major or 'of an unknown version'} at {source}, but "
              f"--require-major {args.require_major} was asked for. Refusing to stage "
              "it: a cluster created by one major version cannot be opened by "
              "another, so shipping the wrong one would break every existing "
              "installation.", file=sys.stderr)
        return 1

    if DESTINATION.exists():
        shutil.rmtree(DESTINATION)

    copied = _copy_bin(source / "bin", DESTINATION / "bin")
    copied += _copy_tree(source / "share", DESTINATION / "share", prune_names=SHARE_PRUNE)
    if (source / "lib").is_dir():
        copied += _copy_tree(source / "lib", DESTINATION / "lib", prune_names=set())

    # Read at runtime by local_server.bundled_major_version(), which compares
    # it against the cluster's PG_VERSION before trying to start the server.
    if major:
        (DESTINATION / VERSION_MARKER).write_text(f"{major}\n", encoding="utf-8")
    else:
        print("Warning: could not determine the PostgreSQL major version, so no "
              f"{VERSION_MARKER} was written. The application will skip its "
              "version-compatibility check.", file=sys.stderr)

    # The PostgreSQL License requires the copyright notice to accompany
    # redistributed binaries.
    for notice in ("COPYRIGHT", "COPYRIGHT.txt"):
        candidate = source / notice
        if candidate.is_file():
            shutil.copy2(candidate, DESTINATION / "POSTGRESQL-COPYRIGHT.txt")
            break

    problems = _verify_staged_share(DESTINATION)
    if problems:
        print(f"The staged tree at {DESTINATION} is not usable:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        # Removed, not left for inspection: a half-staged tree still looks
        # bundled to local_server.is_bundled(), so leaving it behind would
        # let the next build pick it up and ship it.
        shutil.rmtree(DESTINATION, ignore_errors=True)
        print("Removed the incomplete bundle rather than let it be shipped.",
              file=sys.stderr)
        return 1

    size_mb = sum(f.stat().st_size for f in DESTINATION.rglob("*") if f.is_file())
    size_mb /= 1024 * 1024
    print(f"Staged PostgreSQL {major or '?'} from {source} to {DESTINATION} "
          f"({copied} files, {size_mb:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
