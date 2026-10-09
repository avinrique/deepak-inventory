"""The PostgreSQL server the application runs on this computer itself.

Why this exists: a shop that just ran the installer has no database and no
reason to own one. Before this module, the only route from "I installed it"
to "I can log in" was typing a host, port, username and password into the
setup wizard -- which in practice meant signing up for a hosted database,
because a normal Windows PC has no PostgreSQL on it. Here the installer
carries a PostgreSQL server, and this module runs it: initialize a data
directory under the user's own AppData, start it on a free loopback port,
create the database, and stop it again when the app closes. The user never
sees a connection string.

What this is *not*: a shared or networked database. The cluster listens on
127.0.0.1 only -- nothing off the machine can reach it, and Windows
Firewall never has cause to prompt. A second PC needs the remote option in
the setup wizard, pointed at a real server.

Deliberately Qt-free: app.main calls ensure_running() before any window
exists, and the setup wizard calls it from a worker thread.

Two invariants worth keeping in mind when editing:

1. **Every subprocess call passes creationflags=_NO_WINDOW.** The shipped
   .exe is built --windowed, so Windows gives each child process a console
   of its own -- a black box that flashes on screen. That would happen on
   every single launch here, not just during a backup.
2. **ensure_running() persists the DSN it returns.** The port or the
   password can legitimately change (a port taken by something else, a
   cluster whose stored password was lost), and the caller must not be left
   holding a stale URL. Saving is this module's job, not the caller's.
"""
import contextlib
import logging
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.config import store
from app.config.settings import reload_settings, settings
from app.core.exceptions import AppError
from app.core.paths import data_dir, logs_dir, pg_server_dir
from app.database.dsn import build_url

_logger = logging.getLogger(__name__)

# Not "postgres". A cluster this application created and manages is not the
# shared server an administrator might also be pointing other tools at, and
# the role name is the clearest place to say so.
SUPERUSER = "inventory_app"
DATABASE = "inventory"

# 5432 first so a machine with nothing else on it gets the conventional
# port, then a short walk upwards for the common case of a pre-existing
# PostgreSQL installation holding 5432.
PREFERRED_PORTS = (5432, 5433, 5434, 5435, 5436)

# See invariant 1 in the module docstring. Same constant, same reason, as
# app.backup.postgres_backup.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# pg_ctl waits for the server to report itself ready rather than returning
# the moment the process spawns, so this has to cover a crash-recovery
# startup on a slow disk, not just a clean one.
_START_TIMEOUT_SECONDS = 60
_STOP_TIMEOUT_SECONDS = 30
_INITDB_TIMEOUT_SECONDS = 300

# How long a second instance waits for the first to finish initialising.
# Comfortably longer than _INITDB_TIMEOUT_SECONDS: the whole point is to
# outlast the slowest thing the holder of the lock might be doing.
_LOCK_WAIT_SECONDS = 360

_REQUIRED_BINARIES = ("initdb", "pg_ctl", "postgres")

_VERSION_MARKER = "PGSQL_VERSION.txt"

# Written into postgresql.conf once, after initdb. Fenced so that running
# against an already-configured cluster appends nothing.
_CONF_BEGIN = "# --- InventoryManagementSystem managed block (do not edit) ---"
_CONF_END = "# --- end InventoryManagementSystem managed block ---"
_CONF_BLOCK = f"""
{_CONF_BEGIN}
# Loopback only. This is the single line that keeps the database off the
# network, and with it Windows Firewall never has anything to prompt about.
listen_addresses = '127.0.0.1'
# A desktop app with one user; the default 100 backends is pure overhead.
max_connections = 20
shared_buffers = 128MB
# The server's log goes where pg_ctl -l points it, which is the app's own
# log directory. Letting the collector take over would scatter a second set
# of logs inside the data directory instead.
logging_collector = off
{_CONF_END}
"""


class LocalServerError(AppError):
    """The built-in database could not be prepared, started or stopped.

    Carries a message meant to be shown to whoever is sitting in front of
    the machine, which is why the raise sites spell out a remedy rather than
    forwarding postgres' own stderr verbatim.
    """


def _sanitize(output: str) -> str:
    """Defence in depth before any postgres output reaches a dialog.

    The generated superuser password only ever travels through a temporary
    --pwfile, so it should not be in here -- but a libpq error can quote a
    whole connection string, and that one does carry it.
    """
    redacted = re.sub(r"(?i)(postgres(?:ql)?(?:\+\w+)?://)\S*", r"\1[REDACTED]", output)
    redacted = re.sub(r"(?i)(password\s*=\s*)\S+", r"\1[REDACTED]", redacted)
    return redacted.strip()


# -- locations ------------------------------------------------------------ #
def _binary(name: str) -> Path:
    suffix = ".exe" if sys.platform == "win32" else ""
    return pg_server_dir() / "bin" / f"{name}{suffix}"


def pgdata_path() -> Path:
    """The cluster's data directory.

    Under data_dir() (LOCALAPPDATA), never config_dir(): this is large,
    entirely specific to one machine, and must not follow a domain user
    between PCs the way config.json deliberately does.
    """
    return data_dir() / "pgdata"


def _server_log() -> Path:
    return logs_dir() / "pgserver.log"


def _lock_file() -> Path:
    return data_dir() / "pgserver.lock"


def is_bundled() -> bool:
    """True when this build actually shipped a server.

    A build without one still runs -- the setup wizard disables its "on this
    computer" option and explains why, the same way Backup reports missing
    pg_dump rather than failing obscurely.
    """
    return all(_binary(name).is_file() for name in _REQUIRED_BINARIES)


def is_initialized() -> bool:
    """PG_VERSION is the file postgres itself treats as proof that a
    directory is a cluster, so it is the right thing to test."""
    return (pgdata_path() / "PG_VERSION").is_file()


# -- preconditions -------------------------------------------------------- #
def _is_elevated() -> bool:
    """A seam, so the Administrator refusal below is testable off Windows."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - absence of an answer is not elevation
        return False


def _check_not_elevated() -> None:
    """postgres refuses to run under an administrator token -- its own code,
    not a policy of ours, and it is right to: a database server should not
    hold privileges it cannot drop.

    The normal case never trips this. The installer asks for no elevation
    (PrivilegesRequired=lowest) so even an administrator's account runs the
    app with a filtered token. Someone who deliberately picks "Run as
    administrator" would otherwise get postgres' raw refusal, which reads
    like a bug in the application.
    """
    if _is_elevated():
        raise LocalServerError(
            "The built-in database cannot be used while this program is running "
            "as an administrator.\n\nClose it and open it normally — from the "
            "Start Menu or its desktop shortcut — rather than using "
            "\"Run as administrator\".")


def _warn_about_unusual_pgdata(pgdata: Path) -> None:
    """Diagnostics only, never a refusal.

    Both conditions below are survivable on most machines, and refusing
    would break a working installation over a guess. Logging them means a
    report of "it fails on this one PC" is answerable from the log file
    instead of a remote debugging session.
    """
    if not str(pgdata).isascii():
        _logger.warning(
            "The data directory path is not ASCII (%s). PostgreSQL on Windows "
            "has historically been fragile about this; if initdb fails, that is "
            "the first thing to suspect.", pgdata)
    if "onedrive" in str(pgdata).lower():
        _logger.warning(
            "The data directory appears to sit inside OneDrive (%s). A live "
            "database under file sync risks corruption — a remote database is "
            "the safer choice on this machine.", pgdata)


# -- subprocess ----------------------------------------------------------- #
def _run(argv: list[str], timeout: int) -> subprocess.CompletedProcess:
    """Every call into the PostgreSQL binaries goes through here, so the
    no-console flag cannot be forgotten at a new call site."""
    _logger.debug("Running %s", " ".join(argv))
    return subprocess.run(argv, capture_output=True, text=True, check=False,
                          timeout=timeout, creationflags=_NO_WINDOW)


def _tail(path: Path, lines: int = 25) -> str:
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(content.splitlines()[-lines:])


# -- cross-process lock --------------------------------------------------- #
@contextlib.contextmanager
def _instance_lock():
    """Serializes initdb/start across *processes*, not just threads.

    Double-clicking the shortcut twice starts two processes that both see
    "not running" and both run pg_ctl start, each having independently
    chosen a port. One of them wins, the other reports a failure the user
    did nothing to cause. A threading.Lock cannot help -- these are separate
    interpreters -- so this takes an OS-level lock on a file.

    postgres' own postmaster.pid lock still guards the data directory
    against anything that bypasses this.
    """
    _lock_file().parent.mkdir(parents=True, exist_ok=True)
    handle = open(_lock_file(), "a+b")  # noqa: SIM115 - released in the finally
    try:
        if sys.platform == "win32":
            import msvcrt

            # Polled with LK_NBLCK rather than one LK_LOCK call, because
            # LK_LOCK is not the indefinite wait its name suggests: it
            # retries for about ten seconds and then raises. A first-run
            # initdb takes far longer than that, so the second process would
            # give up -- with an OSError surfacing as "could not be
            # started" -- precisely in the case this lock exists to handle.
            deadline = time.monotonic() + _LOCK_WAIT_SECONDS
            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise LocalServerError(
                            "Another copy of this application is still "
                            "setting up the database. Wait for it to finish, "
                            "then try again.") from None
                    time.sleep(0.5)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            if sys.platform == "win32":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:  # pragma: no cover - the handle close releases it anyway
            pass
        handle.close()


# -- version ------------------------------------------------------------- #
def bundled_major_version() -> str | None:
    """The major version staged alongside the binaries, or None when the
    marker is absent (a tree assembled by hand during development).

    Read from a file rather than by running `postgres --version`, because
    this is checked on every single launch and spawning a process to compare
    two short strings is not worth it.
    """
    marker = pg_server_dir() / _VERSION_MARKER
    try:
        return marker.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _cluster_major_version() -> str | None:
    try:
        return (pgdata_path() / "PG_VERSION").read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _check_version_compatible() -> None:
    """postgres will not open a data directory written by a different major
    version -- it enforces that itself, and on-disk formats genuinely
    change between majors.

    Catching it here turns "the application will not start" into a sentence
    that names both versions and says what to do, and it has to happen
    *before* pg_ctl start rather than after, because postgres' own message
    for this lands in a log file nobody is going to read.
    """
    bundled = bundled_major_version()
    cluster = _cluster_major_version()
    if bundled is None or cluster is None or bundled == cluster:
        return
    raise LocalServerError(
        f"The database on this computer was created with PostgreSQL {cluster}, "
        f"but this version of the application includes PostgreSQL {bundled}. "
        "Your data has not been touched, but it cannot be opened until it is "
        "converted.\n\nInstall the previous version of the application again, "
        "take a backup from Settings → Backup, then upgrade and restore it.")


# -- initdb -------------------------------------------------------------- #
def _write_managed_conf() -> None:
    config = pgdata_path() / "postgresql.conf"
    existing = config.read_text(encoding="utf-8")
    if _CONF_BEGIN in existing:
        return
    config.write_text(existing + _CONF_BLOCK, encoding="utf-8")


def _initdb(password: str) -> None:
    """Creates the cluster. Only ever called when PG_VERSION is absent.

    A failure part-way leaves a directory that is neither empty nor a
    cluster, which initdb refuses to touch on the next attempt -- so the
    partial directory is removed here. The PG_VERSION guard makes that safe:
    a real cluster never reaches this code.
    """
    pgdata = pgdata_path()
    pgdata.parent.mkdir(parents=True, exist_ok=True)

    # --pwfile is the only non-interactive way to set the superuser
    # password: initdb otherwise prompts on a terminal this process does not
    # have, and it reads no environment variable for it.
    handle, pwfile = tempfile.mkstemp(dir=str(data_dir()), prefix=".pw-")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(password)
        argv = [
            str(_binary("initdb")),
            "-D", str(pgdata),
            "-U", SUPERUSER,
            "--pwfile", pwfile,
            "--auth=scram-sha-256",
            "-E", "UTF8",
            # Deliberate. With the system locale, initdb on a Windows PC
            # whose code page is not UTF-8 fails outright ("encoding UTF8
            # does not match locale"), which is most of them. C always
            # matches. The cost is that text sorts by byte value, so
            # ORDER BY puts uppercase before lowercase.
            "--locale=C",
            "--no-instructions",
        ]
        _logger.info("Initializing a local database cluster at %s", pgdata)
        result = _run(argv, timeout=_INITDB_TIMEOUT_SECONDS)
    finally:
        Path(pwfile).unlink(missing_ok=True)

    if result.returncode != 0:
        if pgdata.exists() and not (pgdata / "PG_VERSION").is_file():
            shutil.rmtree(pgdata, ignore_errors=True)
        raise LocalServerError(
            "The database on this computer could not be prepared.\n\n"
            + (_sanitize(result.stderr) or "initdb failed without explaining why."))

    _write_managed_conf()


# -- ports --------------------------------------------------------------- #
def _port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        # SO_REUSEADDR is deliberately NOT set: it would let this bind
        # succeed alongside a socket already on the port and report it free.
        try:
            probe.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _choose_port(exclude: int | None = None) -> int:
    for port in PREFERRED_PORTS:
        if port != exclude and _port_is_free(port):
            return port
    # Everything conventional is taken. Let the OS name one rather than
    # refusing to start.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


# -- start / stop -------------------------------------------------------- #
class _PortUnavailable(Exception):
    """Internal: the chosen port was taken between the probe and the bind."""


def status() -> str:
    """"running", "stopped", or "absent" for a directory that is not a
    cluster at all. pg_ctl's exit codes already distinguish these."""
    if not is_initialized():
        return "absent"
    result = _run([str(_binary("pg_ctl")), "status", "-D", str(pgdata_path())],
                  timeout=30)
    return "running" if result.returncode == 0 else "stopped"


def _start(port: int) -> None:
    log = _server_log()
    log.parent.mkdir(parents=True, exist_ok=True)
    argv = [
        str(_binary("pg_ctl")), "start",
        "-D", str(pgdata_path()),
        "-l", str(log),
        # Wait for "ready to accept connections" instead of returning as
        # soon as the process exists, so a caller that connects immediately
        # afterwards does not race startup.
        "-w", "-t", str(_START_TIMEOUT_SECONDS),
        # The port lives here rather than in postgresql.conf so that
        # changing it needs no file rewrite.
        "-o", f"-p {port}",
    ]
    _logger.info("Starting the local database on port %s", port)
    result = _run(argv, timeout=_START_TIMEOUT_SECONDS + 30)
    if result.returncode == 0:
        return

    detail = _tail(log)
    combined = f"{result.stderr}\n{detail}"
    if "Address already in use" in combined or "could not bind" in combined:
        raise _PortUnavailable(port)
    raise LocalServerError(
        "The database on this computer could not be started.\n\n"
        + (_sanitize(combined) or "pg_ctl gave no reason."))


def _start_on_a_free_port(port: int) -> int:
    """Starts the server, returning the port it actually came up on.

    The probe in _choose_port and pg_ctl's own bind are two separate moments,
    and something else on the machine can claim the port in between. Rather
    than trust the probe, treat a bind failure as ordinary and try once more
    somewhere else. Routing every start through here is also what keeps
    _PortUnavailable -- an internal signal with no user-facing message -- from
    ever escaping to a dialog.
    """
    try:
        _start(port)
        return port
    except _PortUnavailable:
        retry = _choose_port(exclude=port)
        _logger.info("Port %s was taken; retrying on %s", port, retry)
        _start(retry)
        return retry


_stopped_already = False


def stop() -> None:
    """Shuts the cluster down. Idempotent, and never raises.

    Called from app shutdown, where an exception has nowhere useful to go
    and the OS is about to reclaim the process anyway -- so a failure is
    logged and swallowed.

    -m fast, not smart: smart waits for every client to disconnect, which on
    a desktop app being closed means waiting for connections that are going
    away with us. fast rolls back what is in flight and exits.
    """
    global _stopped_already
    if _stopped_already or not is_bundled() or not is_initialized():
        return
    _stopped_already = True
    try:
        result = _run([str(_binary("pg_ctl")), "stop", "-D", str(pgdata_path()),
                       "-m", "fast", "-w", "-t", str(_STOP_TIMEOUT_SECONDS)],
                      timeout=_STOP_TIMEOUT_SECONDS + 30)
        if result.returncode != 0:
            _logger.warning("pg_ctl stop exited %s: %s", result.returncode,
                            _sanitize(result.stderr))
        else:
            _logger.info("Local database stopped")
    except Exception as exc:  # noqa: BLE001 - shutdown must not raise
        _logger.warning("Could not stop the local database: %s", exc)


def reset_stop_guard() -> None:
    """Lets a process that stopped the server start it again. Only the tests
    and a retry-after-failure path need this."""
    global _stopped_already
    _stopped_already = False


# -- database ------------------------------------------------------------ #
def local_url(*, port: int, password: str, database: str = DATABASE) -> str:
    """sslmode=disable: the connection never leaves the loopback interface,
    so a certificate would add setup friction and protect nothing."""
    return build_url(host="127.0.0.1", port=port, database=database,
                     username=SUPERUSER, password=password, sslmode="disable")


def _create_database_if_missing(port: int, password: str) -> None:
    """Idempotent on every launch, not just the first.

    psycopg rather than createdb.exe: CREATE DATABASE cannot run inside a
    transaction block, which AUTOCOMMIT handles directly, and this avoids a
    second subprocess plus a second way of handing a password to a child
    process.
    """
    engine = create_engine(local_url(port=port, password=password, database="postgres"),
                           future=True, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            exists = connection.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": DATABASE}).first()
            if exists:
                return
            _logger.info("Creating the %r database", DATABASE)
            # DATABASE is a module constant, never user input -- an
            # identifier cannot be a bind parameter.
            connection.execute(text(f'CREATE DATABASE "{DATABASE}"'))
    except Exception as exc:  # noqa: BLE001 - re-raised with a usable message
        raise LocalServerError(
            "The database on this computer started, but could not be prepared "
            "for use.\n\n" + _sanitize(str(exc))) from exc
    finally:
        engine.dispose()


# -- credentials --------------------------------------------------------- #
def _stored_credentials() -> tuple[int | None, str | None]:
    """The port and password from the saved DSN -- but only if that DSN is
    actually describing *this* cluster.

    The checks are the point of this function, not paranoia. config.json
    holds whatever database the app was last pointed at, which is routinely
    somebody else's server: the user who tried the built-in database, moved
    to a hosted one, and then moved back arrives here with a cloud DSN
    saved. Taking its password on trust means handing a cloud password to
    our own cluster, which rejects it; and since _persist() is only reached
    on success, the bad DSN is never replaced, so every retry fails
    identically and the local data sits on disk permanently unreachable.

    Returning (None, None) instead routes that case into
    _reset_superuser_password(), which is exactly the recovery it exists
    for. A port with no password is no use either -- the port alone cannot
    authenticate -- so both are discarded together.
    """
    if not settings.database_url or not settings.database_managed_locally:
        return None, None
    try:
        url = make_url(settings.database_url)
    except Exception:  # noqa: BLE001 - a corrupt URL is the same as none
        return None, None
    if (url.host not in ("127.0.0.1", "localhost")
            or url.username != SUPERUSER
            or url.database != DATABASE
            or url.port is None
            or not url.password):
        _logger.info("The saved connection does not describe the built-in "
                     "database; treating its credentials as absent")
        return None, None
    return url.port, url.password


def _reset_superuser_password(port: int) -> tuple[int, str]:
    """Gives the cluster a new password when the stored one is gone.

    Returns the port it ended up running on, along with the new password.

    Reachable in a real situation, not a contrived one: config.json lives in
    roaming AppData and pgdata in local AppData, so clearing the former --
    a profile reset, a new Windows user, a hand-cleaned AppData -- leaves a
    perfectly good database whose generated password nobody knows. Without
    this, the shop's data would be sitting on disk and unreachable.

    pg_hba.conf is switched to trust for the one loopback role, the password
    is set, and the file is put straight back. The window is a fraction of a
    second on a port bound to 127.0.0.1, and the alternative is losing the
    data.
    """
    hba = pgdata_path() / "pg_hba.conf"
    original = hba.read_text(encoding="utf-8")
    password = secrets.token_urlsafe(24)
    _logger.warning("The stored password for the local database is missing; "
                    "resetting it so the existing data stays reachable")
    try:
        hba.write_text(f"host all {SUPERUSER} 127.0.0.1/32 trust\n"
                       f"host all {SUPERUSER} ::1/128 trust\n", encoding="utf-8")
        port = _start_on_a_free_port(port)
        engine = create_engine(local_url(port=port, password="", database="postgres"),
                               future=True, isolation_level="AUTOCOMMIT")
        try:
            with engine.connect() as connection:
                # The password has to be a literal, not a bind parameter:
                # ALTER USER is DDL and PostgreSQL rejects a placeholder
                # there outright ("syntax error at or near $1"). psycopg's
                # sql.Literal does the quoting, rather than this trusting
                # that a generated token happens to contain nothing that
                # needs escaping.
                from psycopg import sql

                statement = sql.SQL("ALTER USER {name} WITH PASSWORD {password}").format(
                    name=sql.Identifier(SUPERUSER), password=sql.Literal(password))
                connection.exec_driver_sql(
                    statement.as_string(connection.connection.driver_connection))
        finally:
            engine.dispose()
    finally:
        hba.write_text(original, encoding="utf-8")
        # The server has the permissive rules loaded; it must not keep them.
        stop()
        reset_stop_guard()
    return _start_on_a_free_port(port), password


# -- the entry point ----------------------------------------------------- #
def _persist(dsn: str) -> None:
    """Writes the DSN back to config.json when it has changed.

    See invariant 2 in the module docstring: a port or password that moved
    has to reach the saved configuration, or the next connection attempt
    uses the old one. store.save() splits the password out and encrypts it,
    exactly as it does for a remote database.
    """
    if settings.database_url == dsn:
        return
    from app.database.session import reset_engine

    store.save({**store.load(), "database_url": dsn,
                "database_managed_locally": True})
    reload_settings()
    reset_engine()
    _logger.info("Saved the local database connection: %s", store.redacted_url(dsn))


def ensure_running() -> str:
    """Brings the built-in database up and returns the DSN to use.

    Idempotent and safe to call more than once per process. The common case
    -- every launch after the first -- is one pg_ctl status call and nothing
    else.
    """
    if not is_bundled():
        missing = [n for n in _REQUIRED_BINARIES if not _binary(n).is_file()]
        raise LocalServerError(
            "This copy of the application does not include the built-in "
            f"database ({', '.join(missing)} is missing). Reinstalling should "
            "restore it, or you can connect to a database on another server "
            "instead.")

    _check_not_elevated()
    _warn_about_unusual_pgdata(pgdata_path())

    with _instance_lock():
        if not is_initialized():
            password = secrets.token_urlsafe(24)
            _initdb(password)
            port = _start_on_a_free_port(_choose_port())
        else:
            _check_version_compatible()
            port, password = _stored_credentials()
            running = status() == "running"

            if password is None:
                # A cluster we have no usable credentials for: the stored
                # password is gone, or belongs to a different database
                # entirely. _stored_credentials() discards the port with it,
                # so a fresh one is chosen here. It cannot be reset while it
                # is up, and it is unusable until it is reset.
                if running:
                    stop()
                    reset_stop_guard()
                    running = False
                port, password = _reset_superuser_password(_choose_port())
            elif not running:
                # A port busy while our server is down belongs to something
                # else, so do not try to take it.
                if port is None or not _port_is_free(port):
                    port = _choose_port()
                port = _start_on_a_free_port(port)

        _create_database_if_missing(port, password)

    dsn = local_url(port=port, password=password)
    _persist(dsn)
    return dsn
