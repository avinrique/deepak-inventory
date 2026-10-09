"""app.database.local_server — the bundled PostgreSQL server manager.

Almost everything here runs with ``_run`` replaced by a stub, so the whole
file passes on the macOS machine this is developed on while asserting the
argv shapes that only matter on Windows. The end-to-end behaviour (initdb
actually producing a cluster, pg_ctl actually starting it) is verified in
CI on a Windows runner, which is the only place the shipped binaries exist.

Port selection is tested against real sockets rather than mocks: binding
127.0.0.1 is fast, deterministic and identical across platforms, and a
mocked socket would be testing the mock.
"""
import socket
import subprocess
from pathlib import Path

import pytest

from app.database import local_server


@pytest.fixture
def staged(tmp_path, monkeypatch):
    """A fake bundle and a fake AppData, so nothing touches the real ones.

    local_server did ``from app.core.paths import data_dir``, so the names
    have to be rebound on local_server itself -- patching app.core.paths
    would leave the already-imported references pointing at the originals.
    """
    server = tmp_path / "pgsql"
    (server / "bin").mkdir(parents=True)
    for name in ("initdb", "pg_ctl", "postgres", "pg_dump"):
        (server / "bin" / name).write_text("")
        (server / "bin" / f"{name}.exe").write_text("")

    appdata = tmp_path / "appdata"
    (appdata / "logs").mkdir(parents=True)

    monkeypatch.setattr(local_server, "pg_server_dir", lambda: server)
    monkeypatch.setattr(local_server, "data_dir", lambda: appdata)
    monkeypatch.setattr(local_server, "logs_dir", lambda: appdata / "logs")
    return server, appdata


@pytest.fixture
def calls(monkeypatch):
    """Records every subprocess invocation instead of running it.

    A successful initdb is emulated as far as creating the data directory
    and a postgresql.conf, because that is the part the code afterwards
    depends on: _write_managed_conf appends to the file initdb leaves
    behind. Returning a bare success without it would make these tests pass
    against a module that could not work.
    """
    recorded: list[dict] = []

    def fake_run(argv, timeout):
        recorded.append({"argv": argv, "timeout": timeout})
        if Path(argv[0]).name.startswith("initdb"):
            pgdata = Path(argv[argv.index("-D") + 1])
            pgdata.mkdir(parents=True, exist_ok=True)
            (pgdata / "postgresql.conf").write_text(
                "#listen_addresses = 'localhost'\nmax_connections = 100\n",
                encoding="utf-8")
            (pgdata / "PG_VERSION").write_text("16\n", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(local_server, "_run", fake_run)
    return recorded


def _argv_for(calls: list[dict], program: str) -> list[str]:
    for call in calls:
        if Path(call["argv"][0]).name.startswith(program):
            return call["argv"]
    raise AssertionError(f"{program} was never invoked; got {calls}")


# -- discovery ----------------------------------------------------------- #

def test_is_bundled_is_false_without_the_binaries(tmp_path, monkeypatch):
    """A build assembled without fetch_pgserver.py must degrade, not crash —
    the setup wizard reads this to decide whether to offer the option."""
    monkeypatch.setattr(local_server, "pg_server_dir", lambda: tmp_path / "absent")

    assert local_server.is_bundled() is False


def test_is_bundled_is_true_once_every_required_binary_is_present(staged):
    assert local_server.is_bundled() is True


def test_is_bundled_is_false_when_only_some_binaries_are_present(staged):
    """A partial bundle is a packaging mistake, and claiming it works would
    surface as a cryptic failure halfway through first-run setup."""
    server, _ = staged
    for suffix in ("", ".exe"):
        (server / "bin" / f"initdb{suffix}").unlink()

    assert local_server.is_bundled() is False


def test_pgdata_lives_under_the_machine_local_data_directory(staged):
    """Not config_dir(): that one roams between machines on a domain, and a
    database following a user to another PC would be a disaster."""
    _, appdata = staged

    assert local_server.pgdata_path() == appdata / "pgdata"


# -- no console windows -------------------------------------------------- #

def test_every_subprocess_call_suppresses_the_console_window(staged, monkeypatch):
    """The regression this guards: the shipped .exe is built --windowed, so
    Windows gives each child process its own console — a black box flashing
    on screen at every launch, because status() runs every time."""
    seen: list[dict] = []

    def fake_run(argv, **kwargs):
        seen.append(kwargs)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(local_server.subprocess, "run", fake_run)
    (local_server.pgdata_path()).mkdir(parents=True)
    (local_server.pgdata_path() / "PG_VERSION").write_text("16\n")

    local_server.status()

    assert [k["creationflags"] for k in seen] == [local_server._NO_WINDOW]


def test_output_is_captured_to_a_file_rather_than_a_pipe(staged, monkeypatch):
    """Not a stylistic choice. pg_ctl start leaves a postgres.exe running
    that inherits the pipe handles, so subprocess.run waits for an EOF that
    only comes when the database shuts down — the call hangs until its
    timeout, every launch, on Windows."""
    seen: list[dict] = []

    def fake_run(argv, **kwargs):
        seen.append(kwargs)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(local_server.subprocess, "run", fake_run)
    (local_server.pgdata_path()).mkdir(parents=True)
    (local_server.pgdata_path() / "PG_VERSION").write_text("16\n")

    local_server.status()

    assert "capture_output" not in seen[0], "back on pipes — this will hang"
    assert seen[0]["stdout"] is not None and hasattr(seen[0]["stdout"], "write")
    assert seen[0]["stderr"] is subprocess.STDOUT


def test_a_command_that_hangs_is_reported_not_raised(staged, monkeypatch):
    """A TimeoutExpired escaping _run reached the user as a bare traceback.
    The callers already know how to turn a non-zero result into a readable
    message, so it is returned as one."""
    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(local_server.subprocess, "run", fake_run)

    result = local_server._run([str(local_server._binary("pg_ctl")), "status"], timeout=5)

    assert result.returncode != 0
    assert "did not finish within 5 seconds" in result.stderr


def test_a_hung_start_becomes_a_readable_error(staged, monkeypatch):
    monkeypatch.setattr(local_server.subprocess, "run",
                        lambda argv, **kw: (_ for _ in ()).throw(
                            subprocess.TimeoutExpired(argv, kw["timeout"])))

    with pytest.raises(local_server.LocalServerError, match="could not be started"):
        local_server._start(5432)


# -- initdb -------------------------------------------------------------- #

def test_initdb_argv_pins_encoding_auth_and_locale(staged, calls):
    local_server._initdb("hunter2")

    argv = _argv_for(calls, "initdb")
    assert "--auth=scram-sha-256" in argv
    assert argv[argv.index("-E") + 1] == "UTF8"
    # Not the system locale: initdb refuses UTF8 against a non-UTF8 Windows
    # code page, which is most Windows machines.
    assert "--locale=C" in argv
    assert argv[argv.index("-U") + 1] == local_server.SUPERUSER
    assert argv[argv.index("-D") + 1] == str(local_server.pgdata_path())


def test_initdb_passes_the_password_by_file_and_deletes_it(staged, calls):
    """The password must never be an argv element — every process on the
    machine can read another's command line."""
    captured = {}
    real_run = local_server._run

    def spy(argv, timeout):
        pwfile = argv[argv.index("--pwfile") + 1]
        captured["path"] = pwfile
        captured["contents"] = Path(pwfile).read_text(encoding="utf-8")
        return real_run(argv, timeout)

    local_server._run = spy
    try:
        local_server._initdb("hunter2")
    finally:
        local_server._run = real_run

    assert captured["contents"] == "hunter2"
    assert "hunter2" not in " ".join(calls[0]["argv"])
    assert not Path(captured["path"]).exists(), "the password file outlived initdb"


def test_initdb_writes_the_managed_configuration_block(staged, calls):
    local_server._initdb("hunter2")

    conf = (local_server.pgdata_path() / "postgresql.conf").read_text(encoding="utf-8")
    # The single line that keeps the database off the network.
    assert "listen_addresses = '127.0.0.1'" in conf


def test_the_managed_block_is_not_appended_twice(staged, calls):
    local_server._initdb("hunter2")
    local_server._write_managed_conf()

    conf = (local_server.pgdata_path() / "postgresql.conf").read_text(encoding="utf-8")
    assert conf.count(local_server._CONF_BEGIN) == 1


def test_a_failed_initdb_removes_the_half_built_directory(staged, monkeypatch):
    """initdb refuses to run against a directory that is neither empty nor a
    cluster, so leaving the debris behind makes every later attempt fail."""
    def fake_run(argv, timeout):
        Path(argv[argv.index("-D") + 1]).mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(argv, 1, stdout="",
                                           stderr="initdb: error: nope")

    monkeypatch.setattr(local_server, "_run", fake_run)

    with pytest.raises(local_server.LocalServerError, match="could not be prepared"):
        local_server._initdb("hunter2")

    assert not local_server.pgdata_path().exists()


def test_a_failed_initdb_never_deletes_a_real_cluster(staged, monkeypatch):
    """The safety guard on the cleanup above: PG_VERSION present means this
    is somebody's data, and nothing here may remove it."""
    pgdata = local_server.pgdata_path()
    pgdata.mkdir(parents=True)
    (pgdata / "PG_VERSION").write_text("16\n")

    monkeypatch.setattr(local_server, "_run", lambda argv, timeout:
                        subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom"))

    with pytest.raises(local_server.LocalServerError):
        local_server._initdb("hunter2")

    assert (pgdata / "PG_VERSION").is_file()


# -- ports --------------------------------------------------------------- #

def test_a_bound_port_is_not_reported_free():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        port = held.getsockname()[1]

        assert local_server._port_is_free(port) is False


def test_choose_port_skips_whatever_is_already_taken(monkeypatch):
    monkeypatch.setattr(local_server, "PREFERRED_PORTS", (5432, 5433))
    monkeypatch.setattr(local_server, "_port_is_free", lambda port: port != 5432)

    assert local_server._choose_port() == 5433


def test_choose_port_falls_back_to_an_ephemeral_port(monkeypatch):
    """Every conventional port taken is survivable — the chosen one is saved
    in the DSN, so it does not have to be predictable."""
    monkeypatch.setattr(local_server, "_port_is_free", lambda port: False)

    port = local_server._choose_port()

    assert port > 0


def test_choose_port_honours_the_exclusion(monkeypatch):
    monkeypatch.setattr(local_server, "PREFERRED_PORTS", (5432, 5433))
    monkeypatch.setattr(local_server, "_port_is_free", lambda port: True)

    assert local_server._choose_port(exclude=5432) == 5433


# -- starting ------------------------------------------------------------ #

def test_start_argv_waits_and_sets_the_port(staged, calls):
    local_server._start(5440)

    argv = _argv_for(calls, "pg_ctl")
    assert "start" in argv
    # -w, or a caller that connects straight afterwards races startup.
    assert "-w" in argv
    assert "-o" in argv and argv[argv.index("-o") + 1] == "-p 5440"
    assert argv[argv.index("-l") + 1] == str(local_server.logs_dir() / "pgserver.log")


def test_a_taken_port_is_retried_elsewhere_rather_than_failing(staged, monkeypatch):
    """The probe and pg_ctl's bind are separate moments; something else can
    claim the port in between."""
    attempts: list[int] = []

    def fake_run(argv, timeout):
        port = int(argv[argv.index("-o") + 1].split()[-1])
        attempts.append(port)
        if len(attempts) == 1:
            return subprocess.CompletedProcess(
                argv, 1, stdout="", stderr='could not bind IPv4 address: '
                                           'Address already in use')
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(local_server, "_run", fake_run)
    monkeypatch.setattr(local_server, "_choose_port", lambda exclude=None: 5499)

    assert local_server._start_on_a_free_port(5432) == 5499
    assert attempts == [5432, 5499]


def test_an_unexplained_start_failure_becomes_a_readable_error(staged, monkeypatch):
    monkeypatch.setattr(local_server, "_run", lambda argv, timeout:
                        subprocess.CompletedProcess(argv, 1, stdout="",
                                                    stderr="FATAL: disk is on fire"))

    with pytest.raises(local_server.LocalServerError, match="could not be started"):
        local_server._start(5432)


def test_start_errors_never_leak_a_connection_string(staged, monkeypatch):
    monkeypatch.setattr(
        local_server, "_run", lambda argv, timeout: subprocess.CompletedProcess(
            argv, 1, stdout="",
            stderr="could not connect to postgresql://inventory_app:sekret@h/db"))

    with pytest.raises(local_server.LocalServerError) as caught:
        local_server._start(5432)

    assert "sekret" not in str(caught.value)


# -- elevation ----------------------------------------------------------- #

def test_running_as_administrator_is_not_refused_outright(staged, calls, monkeypatch):
    """An earlier version raised here, and it was wrong. pg_ctl and initdb
    re-launch postgres with a restricted token precisely so an elevated
    session works, so refusing blocked cases that are fine — including every
    Windows CI runner, which is always elevated."""
    monkeypatch.setattr(local_server, "_is_elevated", lambda: True)
    monkeypatch.setattr(local_server, "_create_database_if_missing",
                        lambda port, password: None)
    monkeypatch.setattr(local_server, "_persist", lambda dsn: None)

    local_server.ensure_running()

    assert calls, "nothing ran at all"


def test_a_failure_while_elevated_names_administrator_as_the_likely_cause(
        staged, monkeypatch):
    """The diagnosis survives even though the refusal did not: postgres'
    own wording for this never mentions elevation, so the user is left with
    no idea what to change."""
    monkeypatch.setattr(local_server, "_is_elevated", lambda: True)
    monkeypatch.setattr(local_server, "_run", lambda argv, timeout:
                        subprocess.CompletedProcess(argv, 1, stdout="",
                                                    stderr="FATAL: permission denied"))

    with pytest.raises(local_server.LocalServerError, match="as an administrator"):
        local_server._start(5432)


def test_an_ordinary_failure_does_not_blame_administrator_rights(staged, monkeypatch):
    """Pinning the wrong cause on an unrelated failure sends the user off
    fixing something that was never the problem."""
    monkeypatch.setattr(local_server, "_is_elevated", lambda: False)
    monkeypatch.setattr(local_server, "_run", lambda argv, timeout:
                        subprocess.CompletedProcess(argv, 1, stdout="",
                                                    stderr="FATAL: disk is full"))

    with pytest.raises(local_server.LocalServerError) as caught:
        local_server._start(5432)

    assert "administrator" not in str(caught.value)


# -- version guard ------------------------------------------------------- #

def test_a_cluster_from_another_major_version_is_refused(staged):
    """postgres will not open it either; this just says so in advance, and
    in words that mention Backup rather than a log file nobody reads."""
    server, _ = staged
    (server / local_server._VERSION_MARKER).write_text("17\n")
    local_server.pgdata_path().mkdir(parents=True)
    (local_server.pgdata_path() / "PG_VERSION").write_text("16\n")

    with pytest.raises(local_server.LocalServerError) as caught:
        local_server._check_version_compatible()

    message = str(caught.value)
    assert "16" in message and "17" in message
    assert "Backup" in message


def test_a_matching_major_version_passes(staged):
    server, _ = staged
    (server / local_server._VERSION_MARKER).write_text("16\n")
    local_server.pgdata_path().mkdir(parents=True)
    (local_server.pgdata_path() / "PG_VERSION").write_text("16\n")

    local_server._check_version_compatible()  # must not raise


def test_a_missing_version_marker_does_not_block_startup(staged):
    """A tree staged by hand during development has no marker; refusing to
    start over a check that cannot be performed would be worse."""
    local_server.pgdata_path().mkdir(parents=True)
    (local_server.pgdata_path() / "PG_VERSION").write_text("16\n")

    local_server._check_version_compatible()  # must not raise


# -- stopping ------------------------------------------------------------ #

def test_stop_is_a_no_op_when_there_is_no_cluster(staged, calls):
    local_server.reset_stop_guard()

    local_server.stop()

    assert calls == []


def test_stop_asks_for_a_fast_shutdown(staged, calls):
    local_server.reset_stop_guard()
    local_server.pgdata_path().mkdir(parents=True)
    (local_server.pgdata_path() / "PG_VERSION").write_text("16\n")

    local_server.stop()

    argv = _argv_for(calls, "pg_ctl")
    assert "stop" in argv
    # smart would wait for clients that are going away with us.
    assert argv[argv.index("-m") + 1] == "fast"


def test_stop_twice_only_stops_once(staged, calls):
    """It is wired to both aboutToQuit and atexit on purpose, so it is
    routinely called twice in one process."""
    local_server.reset_stop_guard()
    local_server.pgdata_path().mkdir(parents=True)
    (local_server.pgdata_path() / "PG_VERSION").write_text("16\n")

    local_server.stop()
    local_server.stop()

    assert len(calls) == 1


def test_stop_never_raises(staged, monkeypatch):
    """It runs during shutdown, where an exception has nowhere to go."""
    local_server.reset_stop_guard()
    local_server.pgdata_path().mkdir(parents=True)
    (local_server.pgdata_path() / "PG_VERSION").write_text("16\n")
    monkeypatch.setattr(local_server, "_run",
                        lambda argv, timeout: (_ for _ in ()).throw(OSError("gone")))

    local_server.stop()  # must not raise


# -- the DSN ------------------------------------------------------------- #

def test_the_local_dsn_is_loopback_and_unencrypted():
    from sqlalchemy.engine import make_url

    url = make_url(local_server.local_url(port=5444, password="p"))

    assert url.host == "127.0.0.1"
    # The connection never leaves the machine, so TLS would add setup
    # friction and protect nothing.
    assert url.query["sslmode"] == "disable"
    assert url.database == local_server.DATABASE


def test_a_generated_password_with_url_metacharacters_survives():
    """token_urlsafe avoids most of these, but the DSN assembly must not be
    the thing that depends on that."""
    from sqlalchemy.engine import make_url

    url = make_url(local_server.local_url(port=5444, password="a@b:c/d?e"))

    assert url.password == "a@b:c/d?e"


# -- the cross-process lock ---------------------------------------------- #

def test_the_lock_waits_longer_than_a_first_run_initdb_can_take():
    """Guards a Windows-only trap. msvcrt.locking(LK_LOCK) is not the
    indefinite wait its name implies — it gives up after about ten seconds —
    so the wait here is polled against this deadline instead. If the
    deadline were ever shortened below the initdb timeout, a second instance
    launched during first-run setup would fail with "could not be started"
    in exactly the situation the lock was added to handle."""
    assert local_server._LOCK_WAIT_SECONDS > local_server._INITDB_TIMEOUT_SECONDS


def test_the_lock_serialises_two_holders(staged):
    """On this platform it is flock; on Windows the polled LK_NBLCK loop.
    Either way the second caller must not proceed while the first holds it."""
    import threading

    order: list[str] = []
    first_has_it = threading.Event()
    release = threading.Event()

    def hold():
        with local_server._instance_lock():
            order.append("first-in")
            first_has_it.set()
            release.wait(timeout=5)
            order.append("first-out")

    holder = threading.Thread(target=hold)
    holder.start()
    assert first_has_it.wait(timeout=5), "the first holder never acquired it"

    def contend():
        with local_server._instance_lock():
            order.append("second-in")

    second = threading.Thread(target=contend)
    second.start()
    # The second must still be blocked while the first holds the lock.
    second.join(timeout=0.5)
    assert order == ["first-in"], f"the second holder got in early: {order}"

    release.set()
    holder.join(timeout=5)
    second.join(timeout=5)
    assert order == ["first-in", "first-out", "second-in"]


# -- whose credentials are those? ---------------------------------------- #

def _configured(monkeypatch, url: str, *, managed: bool) -> None:
    from app.config.settings import settings

    monkeypatch.setattr(settings, "database_url", url, raising=False)
    monkeypatch.setattr(settings, "database_managed_locally", managed, raising=False)


def test_stored_credentials_are_used_for_our_own_cluster(monkeypatch):
    _configured(monkeypatch,
                "postgresql+psycopg://inventory_app:ours@127.0.0.1:5433/inventory",
                managed=True)

    assert local_server._stored_credentials() == (5433, "ours")


def test_a_remote_databases_password_is_never_used_on_the_local_cluster(monkeypatch):
    """The lockout bug. Someone who used the built-in database, moved to a
    cloud one and then moved back arrives with a cloud DSN in config.json.
    Trusting its password means handing it to our own cluster, which rejects
    it — and because the DSN is only rewritten on success, every retry fails
    the same way with the data sitting unreachable on disk. Discarding it
    instead routes into the password reset, which recovers."""
    _configured(monkeypatch,
                "postgresql+psycopg://clouduser:cloudpass@db.neon.tech:5432/inventory",
                managed=False)

    assert local_server._stored_credentials() == (None, None)


def test_credentials_for_a_self_hosted_localhost_postgres_are_rejected(monkeypatch):
    """127.0.0.1 is not proof it is ours — the user may have installed
    PostgreSQL themselves. The managed flag is what settles it."""
    _configured(monkeypatch,
                "postgresql+psycopg://postgres:theirs@127.0.0.1:5432/inventory",
                managed=False)

    assert local_server._stored_credentials() == (None, None)


def test_a_port_without_a_password_is_discarded_too(monkeypatch):
    """A port alone cannot authenticate, so keeping it would start a server
    we then could not connect to."""
    _configured(monkeypatch,
                "postgresql+psycopg://inventory_app@127.0.0.1:5433/inventory",
                managed=True)

    assert local_server._stored_credentials() == (None, None)


def test_a_corrupt_saved_url_is_treated_as_absent(monkeypatch):
    _configured(monkeypatch, "not a url at all", managed=True)

    assert local_server._stored_credentials() == (None, None)


# -- the entry point ----------------------------------------------------- #

def test_ensure_running_explains_which_binary_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(local_server, "pg_server_dir", lambda: tmp_path / "absent")

    with pytest.raises(local_server.LocalServerError, match="initdb"):
        local_server.ensure_running()


def test_ensure_running_adopts_a_server_that_is_already_up(staged, monkeypatch):
    """Reachable every launch, and also after the app is killed from Task
    Manager — postgres outlives it, and starting a second one would fail."""
    pgdata = local_server.pgdata_path()
    pgdata.mkdir(parents=True)
    (pgdata / "PG_VERSION").write_text("16\n")

    started: list[int] = []
    monkeypatch.setattr(local_server, "status", lambda: "running")
    monkeypatch.setattr(local_server, "_start", lambda port: started.append(port))
    monkeypatch.setattr(local_server, "_stored_credentials", lambda: (5432, "hunter2"))
    monkeypatch.setattr(local_server, "_create_database_if_missing",
                        lambda port, password: None)
    monkeypatch.setattr(local_server, "_persist", lambda dsn: None)

    dsn = local_server.ensure_running()

    assert started == [], "it started a second server over a running one"
    assert ":5432/" in dsn
