#!/usr/bin/env python3
"""Runs the built-in database through a full first-run cycle, for real.

Everything in tests/database/test_local_server.py mocks ``subprocess``,
because this project is developed on macOS and the binaries that ship are
Windows ones -- so those tests assert argv shapes and never execute initdb.
This script is the other half: it actually creates a cluster, starts it,
creates the database, migrates it, makes an owner, and shuts it down again.

    cd inventory_system
    python scripts/check_local_server.py                 # scratch dir, cleaned up
    python scripts/check_local_server.py --keep          # leave it for inspection

Run by .github/workflows/windows-build.yml, which is the only place the real
Windows behaviour -- the restricted process pg_ctl starts, the no-console
flag, share/ being complete after pruning -- is ever exercised.

Deliberately redirects every writable location to a temporary directory, so
running it on a machine with a real installation cannot touch that
installation's database or configuration.
"""
import argparse
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

# Same as scripts/init_db.py: running "python scripts/check_local_server.py"
# puts scripts/ on sys.path, not the project directory, so app.* would not
# import without this.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _redirect(root: Path) -> None:
    """Points the app's writable locations at ``root``.

    The modules under test did ``from app.core.paths import data_dir``, so
    rebinding app.core.paths would leave those already-bound names alone --
    the attributes have to be replaced on the importing modules themselves.
    """
    from app.config import store
    from app.database import local_server

    (root / "logs").mkdir(parents=True, exist_ok=True)
    local_server.data_dir = lambda: root
    local_server.logs_dir = lambda: root / "logs"
    store.config_file = lambda: root / "config.json"


def _check(root: Path) -> None:
    from app.config import store
    from app.database import bootstrap, local_server
    from app.database.schema_check import check_schema_version

    if not local_server.is_bundled():
        raise SystemExit(
            f"No PostgreSQL server staged at {local_server.pg_server_dir()}. "
            "Run packaging/fetch_pgserver.py first.")

    print(f"bundled PostgreSQL: {local_server.bundled_major_version()}")

    print("\n-- initdb, start, create database --")
    dsn = local_server.ensure_running()
    print(f"   DSN:    {store.redacted_url(dsn)}")
    print(f"   pgdata: {local_server.pgdata_path()}")
    assert local_server.status() == "running", "the server is not running"

    print("\n-- the database is not reachable from the network --")
    conf = (local_server.pgdata_path() / "postgresql.conf").read_text(encoding="utf-8")
    assert "listen_addresses = '127.0.0.1'" in conf, \
        "listen_addresses is not loopback-only — this would expose the database"
    assert conf.count(local_server._CONF_BEGIN) == 1, \
        "the managed configuration block was written more than once"
    print("   listen_addresses = '127.0.0.1'")

    print("\n-- the cluster really does require a password --")
    # The cluster is created trusting loopback, because initdb cannot be
    # given a password under an elevated launch, and _assign_password closes
    # that a moment later. If that ever silently stopped working, the
    # database would sit open to every process on the machine and nothing
    # else here would notice -- the app itself would keep working fine.
    rules = _active_hba_rules(local_server.pgdata_path())
    leaked = [rule for rule in rules if "trust" in rule.split()]
    assert not leaked, f"pg_hba.conf still trusts loopback: {leaked}"
    from sqlalchemy import create_engine as _create_engine
    probe = _create_engine(
        local_server.local_url(port=local_server._running_port(), password=""),
        future=True)
    try:
        with probe.connect():
            raise AssertionError("connected with no password at all")
    except AssertionError:
        raise
    except Exception:
        print("   a passwordless connection is refused")
    finally:
        probe.dispose()

    print("\n-- a second call adopts the running server --")
    again = local_server.ensure_running()
    assert again == dsn, f"the DSN changed on the second call: {again}"
    assert local_server.status() == "running"
    print("   same DSN, no second server")

    print("\n-- migrations and the role catalog --")
    bootstrap.initialize(dsn)
    check_schema_version()
    print("   schema is at head")

    print("\n-- the first owner account --")
    bootstrap.create_first_owner(organization_name="Local Server Check",
                                 full_name="Check Owner",
                                 email="check@example.com",
                                 password="check-password-1")
    assert bootstrap.has_any_users(), "no user was created"
    print("   created")

    _check_recovers_when_the_password_is_lost(root)

    print("\n-- stop --")
    local_server.stop()
    assert local_server.status() == "stopped", \
        "the server outlived stop() — it would be orphaned after the app closes"
    print("   stopped cleanly")

    print("\nlocal database: OK")


def _active_hba_rules(pgdata: Path) -> list[str]:
    """pg_hba.conf minus comments and blank lines.

    The comments initdb writes *document* every auth method by name,
    including "trust", so a plain substring search on the file reports a
    leak that is not there.
    """
    lines = (pgdata / "pg_hba.conf").read_text(encoding="utf-8").splitlines()
    return [line for line in (raw.strip() for raw in lines)
            if line and not line.startswith("#")]


def _check_recovers_when_the_password_is_lost(root: Path) -> None:
    """The cluster must stay reachable when its stored password is gone.

    A real situation, not a contrived one: config.json lives in roaming
    AppData and pgdata in local AppData, so a profile reset, a new Windows
    user, or a hand-cleaned AppData leaves a perfectly good database whose
    generated password nobody knows. So does using the built-in database,
    moving to a hosted one, and moving back — config.json then holds the
    *remote* DSN.

    Checked here rather than left to a unit test because the recovery runs
    real DDL: it was shipped once with the new password as a bind parameter,
    which PostgreSQL rejects outright for ALTER USER, so the entire path
    failed every time and nothing mocked would have noticed.
    """
    from app.config import store
    from app.config.settings import reload_settings
    from app.database import local_server
    from app.database.session import get_session, reset_engine
    from app.models import Organization

    print("\n-- recovery when the stored password is lost --")
    local_server.reset_stop_guard()
    with get_session() as session:
        before = session.query(Organization.name).scalar()

    # Stand in for "the user moved to a hosted database": a saved DSN that
    # is not ours, whose password must never be tried against our cluster.
    store.save({"database_url": "postgresql+psycopg://someone:elses@db.example.com"
                                ":5432/inventory?sslmode=require",
                "database_managed_locally": False})
    reload_settings()
    reset_engine()

    recovered = local_server.ensure_running()
    assert "127.0.0.1" in recovered, "recovery did not return to the local cluster"
    assert "elses" not in recovered, "it reused the unrelated saved password"

    reload_settings()
    reset_engine()
    with get_session() as session:
        after = session.query(Organization.name).scalar()
    assert after == before, f"data lost during recovery: {after!r} != {before!r}"

    # The reset briefly relaxes pg_hba.conf to trust; it must not stay that way.
    leaked = [rule for rule in _active_hba_rules(local_server.pgdata_path())
              if "trust" in rule.split()]
    assert not leaked, f"pg_hba.conf left on trust authentication: {leaked}"

    print(f"   data intact ({before!r}), credentials rebuilt, scram restored")


def _dump_diagnostics(root: Path) -> None:
    """Everything a failure here needs, printed where it can be read.

    This runs on a CI machine that is destroyed minutes later, so anything
    not on stdout is gone. Deliberately defensive: it is an error path, and
    a diagnostic that raises hides the fault it was meant to explain.
    """
    from app.database import local_server

    print("\n" + "=" * 60)
    print("DIAGNOSTICS")
    print("=" * 60)
    try:
        print(f"server dir : {local_server.pg_server_dir()}")
        print(f"bundled    : {local_server.is_bundled()} "
              f"(major {local_server.bundled_major_version()})")
        print(f"pgdata     : {local_server.pgdata_path()} "
              f"(initialised: {local_server.is_initialized()})")
    except Exception as exc:  # noqa: BLE001 - diagnostics must not mask the fault
        print(f"could not describe the bundle: {exc}")

    for label, path in (("server log", root / "logs" / "pgserver.log"),
                        ("startup log", local_server.pgdata_path() / "log")):
        try:
            if path.is_file():
                print(f"\n--- {label} ({path}) ---")
                print(path.read_text(encoding="utf-8", errors="replace")[-4000:])
            elif path.is_dir():
                for entry in sorted(path.iterdir())[-2:]:
                    print(f"\n--- {label}: {entry.name} ---")
                    print(entry.read_text(encoding="utf-8", errors="replace")[-4000:])
            else:
                print(f"\n--- {label}: not present at {path} ---")
        except Exception as exc:  # noqa: BLE001
            print(f"\n--- {label}: unreadable ({exc}) ---")

    try:
        listing = sorted(p.name for p in (root / "pgdata").iterdir())
        print(f"\npgdata contents: {listing}")
    except Exception:  # noqa: BLE001 - absent is itself the answer
        print("\npgdata contents: directory absent")
    print("=" * 60)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--keep", action="store_true",
                        help="Do not delete the scratch data directory.")
    parser.add_argument("--dir", help="Use this directory instead of a temporary one.")
    args = parser.parse_args()

    root = Path(args.dir) if args.dir else Path(tempfile.mkdtemp(prefix="local-db-check-"))
    print(f"scratch directory: {root}")
    _redirect(root)
    try:
        try:
            _check(root)
        except BaseException as exc:
            # Order here is chosen for what survives truncation. CI turns
            # the *tail* of this output into annotations, and GitHub keeps
            # only ten of them -- so the bulky diagnostics go first, the
            # traceback next, and a one-line summary absolutely last, where
            # nothing can push it out of view. Printing the exception first
            # is what the earlier version did, and it was the line that got
            # dropped.
            _dump_diagnostics(root)
            # Explicitly to stdout: the default is stderr, and the two are
            # merged by the shell with no guarantee about interleaving, so
            # the summary below could otherwise land anywhere but last.
            traceback.print_exc(file=sys.stdout)
            print(f"\nRESULT: FAILED — {type(exc).__name__}: {exc}", flush=True)
            return 1
    finally:
        # Never leave a server running behind us, whatever went wrong.
        from app.database import local_server

        local_server.reset_stop_guard()
        local_server.stop()
        if not args.keep and not args.dir:
            shutil.rmtree(root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
