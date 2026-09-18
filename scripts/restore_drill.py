"""Restore drill (#24): restore the latest encrypted backup into a scratch
database and diff content hashes against the source database.

Usage::

    make restore-drill
    python -m scripts.restore_drill --source-url s3://bucket/prefix

The drill:

1. picks the newest ``<backup_dir>/dumps/*.dump.enc``,
2. decrypts and ``pg_restore``-s it into a scratch database on the same
   PostgreSQL server as the diff source (``BACKUP_SOURCE_URL``, falling back
   to ``DATABASE_URL``),
3. computes a canonical content digest (row count + per-row sha256) over
   ``posts``, ``post_revisions`` and ``audit_events`` on both databases,
4. exits 0 only when every digest matches (zero content-hash mismatches) —
   the ticket's restore-drill acceptance criterion.

The scratch database is dropped when the drill finishes (or the drill runs
with ``--keep-cluster`` for debugging).
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import sys
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings

from scripts.pgbackup import (
    BackupError,
    decrypt_dump,
    list_dumps,
    restore_dump,
    store_root,
)

logger = logging.getLogger("scripts.restore_drill")

CORE_TABLES = ("posts", "post_revisions", "audit_events")


def _canonical_digest(url: str) -> dict[str, str]:
    """Return {table: sha256} computed from row count + canonical row digests.

    Deterministic across databases with identical contents: rows are ordered by
    (created_at, id) and hashed per row; the table digest also folds in the row
    count so truncation/insertion bugs are caught.
    """
    from sqlalchemy import create_engine, text

    engine = create_engine(url)
    digests: dict[str, str] = {}
    try:
        with engine.connect() as conn:
            for table in CORE_TABLES:
                rows = conn.execute(text(f"SELECT * FROM {table} ORDER BY created_at, id")).fetchall()
                h = hashlib.sha256()
                h.update(str(len(rows)).encode())
                for row in rows:
                    h.update(repr(tuple(str(v) if v is not None else "" for v in row)).encode())
                digests[table] = h.hexdigest()
    finally:
        engine.dispose()
    return digests


def _mask_url(url: str) -> str:
    from sqlalchemy.engine import make_url

    return make_url(url).render_as_string(hide_password=True)


def _build_scratch_database(source_url: str) -> tuple[str, str]:
    """Create a scratch database next to ``source_url``; return (url, name)."""
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    parsed = make_url(source_url)
    scratch = f"restore_drill_{uuid4().hex[:8]}"
    admin_url = parsed.set(database="postgres")
    engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{scratch}"'))
    finally:
        engine.dispose()
    # NB: ``str(URL)`` renders the password masked as ``***`` — the drill must
    # hand the real password to pg_restore, so render with hide_password=False.
    scratch_url = parsed.set(database=scratch).render_as_string(hide_password=False)
    logger.info("scratch database: %s", _mask_url(scratch_url))
    return scratch_url, scratch


def _drop_scratch(source_url: str, scratch_name: str) -> None:
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    parsed = make_url(source_url)
    admin_url = parsed.set(database="postgres")
    engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{scratch_name}"'))
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Restore drill (#24)")
    parser.add_argument(
        "--source-url",
        default=None,
        help="database to diff against (default: BACKUP_SOURCE_URL or DATABASE_URL)",
    )
    parser.add_argument("--dump", default=None, help="explicit .dump.enc path (default: newest dump)")
    parser.add_argument(
        "--keep-cluster", action="store_true", help="keep the scratch database for inspection"
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    if not settings.backup_passphrase:
        print("BACKUP_PASSPHRASE is not set; refusing to run the restore drill.", file=sys.stderr)
        return 2

    source_url = args.source_url or (settings.backup_source_url or settings.database_url)

    try:
        if args.dump:
            dump_path = Path(args.dump)
        else:
            dumps = list_dumps(settings)
            if not dumps:
                print(
                    f"no backups found under {store_root(settings) / 'dumps'}; run `make backup` first.",
                    file=sys.stderr,
                )
                return 2
            dump_path = dumps[-1]
        logger.info("restoring backup: %s", dump_path.name)

        data = decrypt_dump(dump_path, settings)
        scratch_url, scratch_name = _build_scratch_database(source_url)
        try:
            restore_dump(data, scratch_url)
        except BackupError:
            if not args.keep_cluster:
                _drop_scratch(source_url, scratch_name)
            raise

        print("==> diffing content digests (schema always matches: same dump source)")
        source_digests = _canonical_digest(source_url)
        restored_digests = _canonical_digest(scratch_url)

        mismatches: list[str] = []
        for table in CORE_TABLES:
            tag = "MATCH " if source_digests[table] == restored_digests[table] else "MISMATCH"
            summary = f"  {tag} {table:<18} src={source_digests[table][:12]}"
            summary += f" restored={restored_digests[table][:12]}"
            print(summary)
            if source_digests[table] != restored_digests[table]:
                mismatches.append(table)

        if mismatches:
            print(
                f"\nrestore drill FAILED: content hash mismatches in: {', '.join(mismatches)}",
                file=sys.stderr,
            )
            return 1

        if not args.keep_cluster:
            _drop_scratch(source_url, scratch_name)

        print(f"\nrestore drill OK: {dump_path.name} restored with zero content-hash mismatches")
        return 0

    except BackupError as exc:
        print(f"restore drill failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(name)s %(message)s")
    raise SystemExit(main())
