"""Restore an encrypted pg_dump to a target database (#33).

Usage::

    python -m scripts.restore --dump /path/to/dump.enc --target-url postgresql://...
    python -m scripts.restore --dump /path/to/dump.enc  # uses DATABASE_URL from settings

The dump must have been created by scripts/backup.py (custom format, AES-256 encrypted).
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
    decrypt_dump,
    restore_dump,
)

logger = logging.getLogger("scripts.restore")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Restore encrypted pg_dump to target database (#33)")
    parser.add_argument("--dump", required=True, help="path to .dump.enc file")
    parser.add_argument(
        "--target-url", default=None, help="target database URL (default: DATABASE_URL from settings)"
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    if not settings.backup_passphrase:
        logger.error("BACKUP_PASSPHRASE is not set; refusing to run.")
        print(BACKUP_PASSPHRASE_REQUIRED, file=sys.stderr)
        return 2

    dump_path = Path(args.dump)
    if not dump_path.exists():
        logger.error("Dump file not found: %s", dump_path)
        return 2

    target_url = args.target_url or settings.database_url

    try:
        logger.info("Decrypting dump: %s", dump_path)
        data = decrypt_dump(dump_path, settings)

        logger.info("Restoring to target database...")
        restore_dump(data, target_url)

        logger.info("Restore completed successfully")
        print("restore ok")
        return 0

    except BackupError as exc:
        logger.error("restore failed: %s", exc)
        print(f"restore failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(name)s %(message)s")
    raise SystemExit(main())
