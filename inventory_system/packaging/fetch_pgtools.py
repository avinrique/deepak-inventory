#!/usr/bin/env python3
"""Stages pg_dump.exe / pg_restore.exe into packaging/pgtools/ for bundling.

Settings -> Backup shells out to these two programs. They are part of the
PostgreSQL *client* tools, and a machine running this application as a
client has no reason to have PostgreSQL installed — so on a normal Windows
PC the Backup and Restore features were simply dead. Shipping the two
binaries with the app fixes that; it is roughly 20 MB, and the alternative
is a business with no working backup.

    python packaging/fetch_pgtools.py                 # find PostgreSQL automatically
    python packaging/fetch_pgtools.py --source "C:\\Program Files\\PostgreSQL\\16\\bin"

The build works without this — the .spec skips an empty pgtools/, and the
app then reports that the tools are missing rather than failing obscurely.
CI runs it so releases include them.

Licensing: PostgreSQL is distributed under the PostgreSQL License, which is
permissive and allows redistribution provided the copyright notice travels
with it. This copies COPYRIGHT from the installation when one is present.
"""
import argparse
import shutil
import sys
from pathlib import Path

DESTINATION = Path(__file__).resolve().parent / "pgtools"

REQUIRED = ["pg_dump", "pg_restore"]

# Windows installations put the DLLs the tools link against in the same bin
# directory (libpq, OpenSSL, ICU, zlib, gettext...). Rather than resolve the
# import table, copy them all: guessing wrong produces an executable that
# fails to start with an unhelpful system dialog, and the whole set is small.
COPY_ALL_SUFFIXES = {".dll"}


def _candidate_dirs() -> list[Path]:
    roots = [Path(r"C:\Program Files\PostgreSQL"),
             Path(r"C:\Program Files (x86)\PostgreSQL")]
    found: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        # Newest major version first.
        for version_dir in sorted(root.iterdir(), reverse=True):
            binary_dir = version_dir / "bin"
            if binary_dir.is_dir():
                found.append(binary_dir)
    # Also honour whatever is on PATH (covers Homebrew/apt for a dry run).
    located = shutil.which("pg_dump")
    if located:
        found.append(Path(located).parent)
    return found


def _executable_names(directory: Path) -> list[str]:
    suffix = ".exe" if sys.platform == "win32" else ""
    return [f"{name}{suffix}" for name in REQUIRED]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", help="PostgreSQL bin directory to copy from.")
    parser.add_argument("--optional", action="store_true",
                        help="Exit 0 instead of 1 when no PostgreSQL is found.")
    args = parser.parse_args()

    candidates = [Path(args.source)] if args.source else _candidate_dirs()
    source = next((d for d in candidates
                   if all((d / name).is_file() for name in _executable_names(d))), None)
    if source is None:
        message = ("Could not find pg_dump/pg_restore. Install the PostgreSQL client "
                   "tools, or pass --source with the directory containing them.")
        if args.optional:
            print(f"Skipping: {message}")
            return 0
        print(message, file=sys.stderr)
        return 1

    DESTINATION.mkdir(parents=True, exist_ok=True)
    copied = 0
    for name in _executable_names(source):
        shutil.copy2(source / name, DESTINATION / name)
        copied += 1
    for item in source.iterdir():
        if item.is_file() and item.suffix.lower() in COPY_ALL_SUFFIXES:
            shutil.copy2(item, DESTINATION / item.name)
            copied += 1

    # The PostgreSQL License requires the copyright notice to accompany
    # redistributed binaries.
    for notice in ("COPYRIGHT", "COPYRIGHT.txt"):
        candidate = source.parent / notice
        if candidate.is_file():
            shutil.copy2(candidate, DESTINATION / "POSTGRESQL-COPYRIGHT.txt")
            break

    size_mb = sum(f.stat().st_size for f in DESTINATION.iterdir()) / (1024 * 1024)
    print(f"Copied {copied} file(s) from {source} to {DESTINATION} ({size_mb:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
