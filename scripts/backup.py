"""Nightly backup job (#24).

Usage::

    make backup                 # run once (dumps + encrypt + push to store)
    python -m scripts.backup     # same
    python -m scripts.backup --prune-only

Writes an AES-256-encrypted ``pg_dump -Fc`` into ``<backup_dir>/dumps/``,
pushes a copy to the S3-ish store when ``BACKUP_STORE_URL=s3://…``, applies
retention (30 daily + 12 monthly), and records outcomes into the
``agentcms_backup_*`` Prometheus series.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings

from scripts.pgbackup import (
    BACKUP_PASSPHRASE_REQUIRED,
    BackupError,
    _is_s3,
    dump_stem,
    list_dumps,
    now_utc,
    prune_local,
    run_dump,
    s3_upload,
    store_root,
)

logger = logging.getLogger("scripts.backup")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Nightly encrypted Postgres backup (#24)")
    parser.add_argument("--prune-only", action="store_true", help="only apply retention, no new dump")
    args = parser.parse_args(argv)

    settings = get_settings()
    if not settings.backup_passphrase:
        logger.error("BACKUP_PASSPHRASE is not set; refusing to run.")
        print(BACKUP_PASSPHRASE_REQUIRED, file=sys.stderr)
        return 2

    from app.observability import metrics

    try:
        if args.prune_only:
            removed = prune_local(settings)
            logger.info("pruned %d stale dumps", len(removed))
            return 0

        dumps_dir = store_root(settings) / "dumps"
        dumps_dir.mkdir(parents=True, exist_ok=True)
        stem = dump_stem(now_utc())
        out_path = dumps_dir / f"{stem}.dump.enc"

        elapsed = run_dump(settings, out_path)
        logger.info("backup written %s (%.1fs)", out_path.name, elapsed)
        print(f"backup ok: {out_path.name} ({elapsed:.1f}s)")

        if _is_s3(settings):
            key = s3_upload(settings, out_path)
            logger.info("uploaded copy to object store: %s", key)

        removed = prune_local(settings)
        metrics.observe_backup_result(True, elapsed)
        print(f"retention: pruned {len(removed)} stale dumps; total kept: {len(list_dumps(settings))}")
        return 0

    except BackupError as exc:
        logger.error("backup failed: %s", exc)
        metrics.observe_backup_result(False, 0.0)
        print(f"backup failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(name)s %(message)s")
    raise SystemExit(main())
