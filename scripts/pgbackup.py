"""Postgres backup/restore library (#24).

Responsibilities:

* ``pg_dump`` in custom format, AES-256-encrypted with ``openssl enc``
* optional re-upload to an S3-compatible object store (SigV4 PUT via the
  media stack's presigned-URL helper)
* local retention: keep the newest N daily snapshots + monthly ones
  (first-of-month) — the S3-pruning story is documented in the runbook
* scratch-cluster helpers for the restore drill (initdb/createdb/pg_restore)

Command-line programs: ``scripts/backup.py`` (run the nightly job) and
``scripts/restore_drill.py`` (restore the latest backup into a scratch DB and
diff content hashes against ``BACKUP_SOURCE_URL`` / ``DATABASE_URL``).
"""

from __future__ import annotations

import contextlib
import logging
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from app.config import Settings

logger = logging.getLogger("app.backup")

DUMP_DIR_NAME = "dumps"
MANIFEST_NAME = "manifest.json"

BACKUP_PASSPHRASE_REQUIRED = (
    "BACKUP_PASSPHRASE must be set (e.g. in .env) before backups can run.\n"
    'Generate one with: python -c "import secrets; print(secrets.token_urlsafe(48))"'
)

SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")


class BackupError(RuntimeError):
    """Raised when a backup/restore operation fails."""


def now_utc() -> datetime:
    return datetime.now(UTC)


def dump_stem(ts: datetime) -> str:
    """Lexicographically sortable dump stem: ``YYYYMMDD-HHMMSS``."""
    return ts.strftime("%Y%m%d-%H%M%S")


def _require_binaries() -> None:
    for binary in ("pg_dump", "pg_restore", "psql", "openssl"):
        if shutil.which(binary) is None:
            raise BackupError(f"required binary not found on PATH: {binary}")


def to_libpq_url(url: str) -> str:
    """Convert a SQLAlchemy URL (e.g. ``postgresql+psycopg://``) to libpq form.

    ``pg_dump``/``pg_restore`` speak libpq; they reject the driver-suffixed
    URL.  ``+psycopg``/``+psycopg2`` are simply chopped off.
    """
    from sqlalchemy.engine import make_url

    return make_url(url).set(drivername="postgresql").render_as_string(hide_password=False)


# ---------------------------------------------------------------------------
# Store abstraction (local file:// only for v1; s3 re-upload uses presigned)
# ---------------------------------------------------------------------------


def store_root(settings: Settings) -> Path:
    """The local directory backups live in."""
    root = getattr(settings, "backup_dir", ".backups") or ".backups"
    if settings.backup_store_url and settings.backup_store_url.startswith("file://"):
        root = settings.backup_store_url[len("file://") :]
    return Path(root)


def _is_s3(settings: Settings) -> bool:
    return bool(settings.backup_store_url and settings.backup_store_url.startswith("s3://"))


def _s3_bucket_prefix(settings: Settings) -> tuple[str, str]:
    url = settings.backup_store_url[len("s3://") :]
    bucket, _, prefix = url.partition("/")
    return bucket, prefix


def encrypt_bytes(data: bytes, passphrase: str) -> bytes:
    """AES-256-CBC encrypt ``data`` with ``passphrase`` via openssl."""
    if not passphrase:
        raise BackupError("BACKUP_PASSPHRASE is not set; refusing to run an unencrypted backup.")
    proc = subprocess.run(
        ["openssl", "enc", "-aes-256-cbc", "-a", "-pbkdf2", "-pass", f"pass:{passphrase}"],
        input=data,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise BackupError(f"openssl encrypt failed: {proc.stderr.decode()[-400:]}")
    return proc.stdout


def decrypt_bytes(data: bytes, passphrase: str) -> bytes:
    """AES-256-CBC decrypt ``data`` (inverse of :func:`encrypt_bytes`)."""
    proc = subprocess.run(
        ["openssl", "enc", "-d", "-aes-256-cbc", "-a", "-pbkdf2", "-pass", f"pass:{passphrase}"],
        input=data,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise BackupError(f"openssl decrypt failed: {proc.stderr.decode()[-400:]}")
    return proc.stdout


# ---------------------------------------------------------------------------
# Dump / restore primitives
# ---------------------------------------------------------------------------


def run_dump(settings: Settings, out_enc_path: Path) -> float:
    """Dump the database (custom format), encrypt it, write ``out_enc_path``.

    Returns elapsed seconds.
    """
    _require_binaries()
    started = now_utc()
    url = to_libpq_url(settings.database_url)
    cmd = [
        "pg_dump",
        "--no-owner",
        "--no-privileges",
        "-Fc",
        url,
    ]
    proc = subprocess.run(cmd, capture_output=True, check=False)
    if proc.returncode != 0:
        raise BackupError(f"pg_dump failed: {proc.stderr.decode()[-800:]}")

    encrypted = encrypt_bytes(proc.stdout, settings.backup_passphrase)
    out_enc_path.write_bytes(encrypted)
    elapsed = (now_utc() - started).total_seconds()
    return elapsed


def s3_upload(settings: Settings, file_path: Path) -> str:
    """Upload an (encrypted) dump to the configured S3-ish store via a
    presigned PUT URL (reusing the media stack's SigV4 code).

    The file on disk (under ``backup_dir``) is the operationally current
    encrypted copy; the S3 copy holds the off-site snapshot.  Restore drills
    always run from the local encrypted dump, so nothing more than a PUT is
    required for v1 (retention on S3 is delegated to bucket lifecycle rules —
    see docs/ops/runbook.md).
    """
    _bucket, prefix = _s3_bucket_prefix(settings)
    key = f"{prefix.rstrip('/')}/{file_path.name}".strip("/") if prefix else file_path.name
    from app.services.media import generate_presigned_put_url

    upload_url, upload_headers, _expires = generate_presigned_put_url(key, "application/octet-stream")

    import httpx

    data = file_path.read_bytes()
    with httpx.Client(timeout=120) as client:
        resp = client.put(upload_url, headers=upload_headers, content=data)
    if resp.status_code >= 300:
        raise BackupError(f"s3 upload of {key} failed: {resp.status_code} {resp.text[:200]}")
    return key


# ---------------------------------------------------------------------------
# Retention (local file store)
# ---------------------------------------------------------------------------


def list_dumps(settings: Settings) -> list[Path]:
    """All ``*.dump.enc`` files in the local store, oldest first."""
    root = store_root(settings) / DUMP_DIR_NAME
    if not root.is_dir():
        return []
    return sorted(
        (p for p in root.iterdir() if p.name.endswith(".dump.enc")),
        key=lambda p: p.name.lower(),
    )


def prune_local(
    settings: Settings, *, keep_daily: int | None = None, keep_monthly: int | None = None
) -> list[Path]:
    """Retain daily + monthly snapshots, removing older ones.

    Retention: the newest ``keep_daily`` dumps are always kept; dumps whose
    day-of-month is the 1st (monthly snapshots) are kept even when older, up to
    ``keep_monthly`` of them.  Returns the removed paths.
    """
    keep_daily = settings.backup_retention_daily if keep_daily is None else keep_daily
    keep_monthly = settings.backup_retention_monthly if keep_monthly is None else keep_monthly

    dumps = sorted(list_dumps(settings), key=lambda p: p.name.lower())  # oldest first
    removed: list[Path] = []
    if not dumps:
        return removed

    # Keep set = newest keep_daily entries + newest keep_monthly monthly entries.
    keep: set[Path] = set(dumps[-keep_daily:]) if keep_daily > 0 else set()
    # ``dump_stem`` is ``%Y%m%d-%H%M%S``: day-of-month is characters 6..8.
    monthlies: list[Path] = [p for p in dumps if p.name[6:8] == "01"]
    keep.update(monthlies[-keep_monthly:] if keep_monthly > 0 else [])

    for path in dumps:
        if path not in keep:
            with contextlib.suppress(FileNotFoundError):  # race with a concurrent backup
                path.unlink()
            removed.append(path)
    return removed


# ---------------------------------------------------------------------------
# Restore-drill support
# ---------------------------------------------------------------------------


def decrypt_dump(path: Path, settings: Settings) -> bytes:
    return decrypt_bytes(path.read_bytes(), settings.backup_passphrase)


def restore_dump(data: bytes, target_url: str) -> None:
    """Restore the custom-format dump (schema + data) into ``target_url``.

    The target must be an empty scratch database created by the drill.
    ``pg_restore`` refuses ``-`` as a stdin filename, so the decrypted dump is
    staged in a temp file first.

    The restore goes through ``pg_restore -f -`` (generate SQL to stdout) +
    ``psql`` rather than ``pg_restore -d`` because pg_restore >= 17 embeds
    ``SET transaction_timeout = 0;`` in its output unconditionally while that
    parameter only exists on Postgres >= 17 — a 17/18 client restoring into a
    16 server would otherwise fail on that one housekeeping statement (it only
    disables a PG17+ default timeout; it is a no-op on older servers).  The
    offending line is stripped before replay.  ``--exit-on-error`` keeps the
    generation strict and ``psql -v ON_ERROR_STOP=1`` aborts on any real SQL
    error, so the drill's guarantees are preserved.
    """
    import subprocess
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".dump", delete=False) as staged:
        staged.write(data)
        staged_path = staged.name
    try:
        gen = subprocess.run(
            [
                "pg_restore",
                "--no-owner",
                "--no-privileges",
                "--exit-on-error",
                "-f",
                "-",
                staged_path,
            ],
            capture_output=True,
            check=False,
        )
        if gen.returncode != 0:
            raise BackupError(f"pg_restore generate failed: {gen.stderr.decode()[-800:]}")
        guarded = subprocess.run(
            [
                "psql",
                "--no-psqlrc",
                "-q",
                "-v",
                "ON_ERROR_STOP=1",
                to_libpq_url(target_url),
            ],
            input=b"\n".join(
                line
                for line in gen.stdout.replace(b"\r\n", b"\n").split(b"\n")
                if line.strip() != b"SET transaction_timeout = 0;"
            ),
            capture_output=True,
            check=False,
        )
        if guarded.returncode != 0:
            raise BackupError(f"pg_restore failed: {guarded.stderr.decode()[-800:]}")
    finally:
        Path(staged_path).unlink(missing_ok=True)


def scratch_database_paths(scratch_dir: Path) -> tuple[Path, Path]:
    """Return (pgdata, logfile) paths for a scratch cluster."""
    pgdata = scratch_dir / "pgdata"
    log = scratch_dir / "postgres.log"
    return pgdata, log


def init_scratch_cluster(scratch_dir: Path) -> Path:
    """initdb a scratch cluster (unprivileged), returns the PGDATA path."""
    pgdata, _log = scratch_database_paths(scratch_dir)
    initdb = shutil.which("initdb")
    if initdb is None:
        raise BackupError("initdb not found on PATH; cannot run the restore drill.")
    if pgdata.exists():
        shutil.rmtree(pgdata)
    pgdata.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [initdb, "-D", str(pgdata), "-U", "postgres", "--no-instructions"],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise BackupError(f"initdb failed: {proc.stderr.decode()[-800:]}")
    return pgdata
