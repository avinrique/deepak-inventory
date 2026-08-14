"""pg_dump/pg_restore process wrapper — the only place in this codebase
that shells out to run a database backup or restore.

Credential safety (a hard requirement, not a nicety): the database
password is NEVER placed on the subprocess command line (visible to any
other process on the machine via `ps`) and NEVER written to a log line.
It is passed exclusively through the `PGPASSWORD` environment variable of
the child process, which only that process (and this one, which already
has it) can see. Every place that might otherwise leak it — logged
commands, sanitized stderr, exceptions — is built to exclude it; see
``_sanitize_output`` and ``_loggable_command``.

Backups are taken with ``pg_dump -F c`` (custom format): it's the only
format ``pg_restore --list`` can introspect without touching a live
database, which is what makes offline, credential-free backup
verification possible.
"""
import hashlib
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy.engine import make_url

from app.domain.backup import format_backup_filename

logger = logging.getLogger(__name__)

_SUBPROCESS_TIMEOUT_SECONDS = 30 * 60  # generous — real databases can be large
_CHECKSUM_CHUNK_SIZE = 1024 * 1024


class BackupToolNotFoundError(Exception):
    """pg_dump/pg_restore isn't on PATH — nothing ran."""


@dataclass(frozen=True)
class BackupOutcome:
    success: bool
    file_path: str
    filename: str
    file_size_bytes: int | None
    checksum_sha256: str | None
    verified: bool
    error_message: str | None


@dataclass(frozen=True)
class RestoreOutcome:
    success: bool
    error_message: str | None


@dataclass(frozen=True)
class _ConnectionParams:
    host: str
    port: int
    user: str
    password: str | None
    database: str


def _connection_params(database_url: str) -> _ConnectionParams:
    url = make_url(database_url)
    return _ConnectionParams(
        host=url.host or "localhost", port=url.port or 5432,
        user=url.username or os.environ.get("USER", "postgres"),
        password=url.password, database=url.database or "postgres")


def _subprocess_env(password: str | None) -> dict[str, str]:
    env = os.environ.copy()
    if password:
        env["PGPASSWORD"] = password
    else:
        env.pop("PGPASSWORD", None)
    return env


def _loggable_command(argv: list[str]) -> str:
    """The argv is safe to log as-is — the password never appears there,
    only in the child process's environment (see module docstring) — but
    building this explicitly (rather than logging argv directly at call
    sites) keeps that invariant in one place if the argv shape ever grows
    a flag that could carry a secret.
    """
    return " ".join(argv)


def _sanitize_output(text: str) -> str:
    """Defense in depth: pg_dump/pg_restore stderr should never contain the
    password (it's never on the command line, and neither tool echoes
    PGPASSWORD), but a misconfigured environment could still print a full
    connection string via a libpq error message. Redact anything shaped
    like one before it's stored or displayed.
    """
    import re

    redacted = re.sub(r"(?i)(postgres(?:ql)?://)[^\s]*", r"\1[REDACTED]", text)
    redacted = re.sub(r"(?i)(password\s*=\s*)\S+", r"\1[REDACTED]", redacted)
    return redacted.strip()


def _require_tool(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise BackupToolNotFoundError(
            f"{name} was not found on PATH — install the PostgreSQL client tools.")
    return path


def _unique_backup_path(backup_dir: Path, filename: str) -> Path:
    """Never overwrites an existing file: format_backup_filename is already
    second-granularity-unique in the common case, but two backups
    triggered within the same second (or a filename collision from a
    prior run) fall back to a numeric suffix instead of clobbering
    whatever's already there.
    """
    candidate = backup_dir / filename
    if not candidate.exists():
        return candidate
    stem, suffix = candidate.stem, candidate.suffix
    n = 1
    while True:
        candidate = backup_dir / f"{stem}_{n}{suffix}"
        if not candidate.exists():
            return candidate
        n += 1


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHECKSUM_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_backup_file(backup_file_path: str) -> bool:
    """Reads the dump's internal table of contents via `pg_restore --list`
    — no database connection, no credentials involved at all. A backup
    that fails this is corrupt or truncated and must not be offered for
    restore.
    """
    pg_restore = _require_tool("pg_restore")
    try:
        result = subprocess.run([pg_restore, "--list", backup_file_path],
                                capture_output=True, text=True,
                                timeout=_SUBPROCESS_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and bool(result.stdout.strip())


def create_backup(database_url: str, backup_dir: str) -> BackupOutcome:
    pg_dump = _require_tool("pg_dump")
    params = _connection_params(database_url)

    directory = Path(backup_dir)
    directory.mkdir(parents=True, exist_ok=True)

    filename = format_backup_filename(params.database, datetime.now())
    out_path = _unique_backup_path(directory, filename)

    argv = [pg_dump, "-h", params.host, "-p", str(params.port), "-U", params.user,
           "-d", params.database, "-F", "c", "-f", str(out_path)]
    logger.info("Running backup: %s", _loggable_command(argv))

    try:
        result = subprocess.run(argv, env=_subprocess_env(params.password),
                                capture_output=True, text=True,
                                timeout=_SUBPROCESS_TIMEOUT_SECONDS, check=False)
    except subprocess.TimeoutExpired:
        out_path.unlink(missing_ok=True)
        message = "Backup timed out."
        logger.error(message)
        return BackupOutcome(False, str(out_path), out_path.name, None, None, False, message)
    except OSError as exc:
        out_path.unlink(missing_ok=True)
        message = _sanitize_output(str(exc))
        logger.error("Backup failed to start: %s", message)
        return BackupOutcome(False, str(out_path), out_path.name, None, None, False, message)

    if result.returncode != 0:
        out_path.unlink(missing_ok=True)
        message = _sanitize_output(result.stderr) or "pg_dump exited with a non-zero status."
        logger.error("Backup failed: %s", message)
        return BackupOutcome(False, str(out_path), out_path.name, None, None, False, message)

    if not out_path.exists() or out_path.stat().st_size == 0:
        message = "pg_dump reported success but produced no output file."
        logger.error(message)
        return BackupOutcome(False, str(out_path), out_path.name, None, None, False, message)

    verified = verify_backup_file(str(out_path))
    if not verified:
        message = "Backup file failed verification (pg_restore --list could not read it)."
        logger.error(message)
        return BackupOutcome(False, str(out_path), out_path.name, out_path.stat().st_size,
                             None, False, message)

    logger.info("Backup completed and verified: %s", out_path.name)
    return BackupOutcome(True, str(out_path), out_path.name, out_path.stat().st_size,
                        _sha256_of(out_path), True, None)


def restore_backup(database_url: str, backup_file_path: str) -> RestoreOutcome:
    """--clean --if-exists drops existing objects before recreating them so
    the restore fully replaces current data rather than merging with it —
    the only sane semantics for "restore this backup", and consistent with
    the caller (BackupService) requiring an explicit, typed confirmation
    before this ever runs.
    """
    pg_restore = _require_tool("pg_restore")
    params = _connection_params(database_url)

    if not Path(backup_file_path).exists():
        message = "Backup file does not exist."
        logger.error(message)
        return RestoreOutcome(False, message)

    argv = [pg_restore, "-h", params.host, "-p", str(params.port), "-U", params.user,
           "-d", params.database, "--clean", "--if-exists", backup_file_path]
    logger.info("Running restore: %s", _loggable_command(argv))

    try:
        result = subprocess.run(argv, env=_subprocess_env(params.password),
                                capture_output=True, text=True,
                                timeout=_SUBPROCESS_TIMEOUT_SECONDS, check=False)
    except subprocess.TimeoutExpired:
        message = "Restore timed out."
        logger.error(message)
        return RestoreOutcome(False, message)
    except OSError as exc:
        message = _sanitize_output(str(exc))
        logger.error("Restore failed to start: %s", message)
        return RestoreOutcome(False, message)

    if result.returncode != 0:
        # pg_restore exits non-zero even for harmless "does not exist,
        # skipping" NOTICEs under --if-exists on a fresh database — only
        # treat it as a real failure if stderr has no successful-looking
        # content beyond those notices.
        stderr = result.stderr or ""
        real_errors = [line for line in stderr.splitlines()
                      if line.strip() and "does not exist, skipping" not in line]
        if real_errors:
            message = _sanitize_output("\n".join(real_errors))
            logger.error("Restore failed: %s", message)
            return RestoreOutcome(False, message)

    logger.info("Restore completed.")
    return RestoreOutcome(True, None)
