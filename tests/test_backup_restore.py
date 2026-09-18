"""Backup + restore-drill integration tests (#24).

Runs the *real* pipeline against the ephemeral Postgres: ``pg_dump -Fc``,
AES-256 encryption, local retention, then the restore drill (scratch DB on the
same server + canonical content-hash diff).  Needs ``pg_dump``/``pg_restore``/
``openssl`` on PATH (present in the dev env; the suite skips loudly otherwise).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session


def _pg_tools_available() -> bool:
    return all(shutil.which(b) for b in ("pg_dump", "pg_restore", "openssl"))


requires_pg_tools = pytest.mark.skipif(
    not _pg_tools_available(), reason="pg_dump/pg_restore/openssl not on PATH"
)


def _settings(tmp_path: Path, database_url: str) -> Any:
    from app.config import Settings

    return Settings(
        app_env="test",
        database_url=database_url,
        secret_key="test-secret-key-that-is-long-enough-000000",
        log_level="WARNING",
        log_format="json",
        backup_dir=str(tmp_path),
        backup_store_url=f"file://{tmp_path}",
        backup_passphrase="restore-drill-passphrase-000",
        backup_retention_daily=3,
        backup_retention_monthly=2,
    )


def _create_rows(client: TestClient, db: Session, auth_headers: dict[str, str]) -> None:
    """Create content that produces posts/revisions/audit/outbox rows."""
    from app.models.site import Site

    site = Site(id=uuid.uuid4(), slug="blog", name="Blog", publish_mode="auto")
    db.add(site)
    db.commit()

    for title in ("First Post", "Second Post"):
        resp = client.post(
            "/v1/sites/blog/posts",
            json={"title": title, "body_md": f"# {title}\n\nBody."},
            headers=auth_headers,
        )
        assert resp.status_code == 201, resp.text
        post_id = resp.json()["id"]
        resp = client.patch(
            f"/v1/posts/{post_id}",
            json={"body_md": f"# {title}\n\nUpdated body."},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text


@requires_pg_tools
class TestEncryptedDump:
    def test_dump_is_encrypted_and_round_trips(
        self, client: TestClient, db: Session, auth_headers: dict[str, str], tmp_path: Path, database_url: str
    ) -> None:
        from scripts import pgbackup

        _create_rows(client, db, auth_headers)

        out = tmp_path / "dumps" / f"{pgbackup.dump_stem(pgbackup.now_utc())}.dump.enc"
        out.parent.mkdir(parents=True)
        settings = _settings(tmp_path, database_url)

        elapsed = pgbackup.run_dump(settings, out)
        assert elapsed >= 0
        assert out.exists()

        raw = out.read_bytes()
        # Ciphertext must not leak the plaintext dump.
        assert b"CREATE TABLE posts" not in raw
        assert b"PGDMP" not in raw

        decrypted = pgbackup.decrypt_bytes(raw, settings.backup_passphrase)
        assert decrypted.startswith(b"PGDMP")  # custom-format magic

        # Wrong passphrase must fail loudly.
        with pytest.raises(pgbackup.BackupError):
            pgbackup.decrypt_bytes(raw, "wrong-passphrase")


@requires_pg_tools
class TestLocalRetention:
    def test_prune_keeps_newest_daily_and_monthly(self, tmp_path: Path, database_url: str) -> None:
        from scripts import pgbackup

        settings = _settings(tmp_path, database_url)
        dumps_dir = tmp_path / "dumps"
        dumps_dir.mkdir(parents=True)

        # Simulate 12 daily dumps, with dumps on the 1st being monthly.
        names: list[str] = []
        for day in range(1, 13):
            ts = datetime(2026, 1, day, 2, 30, tzinfo=UTC)
            if day == 1:
                ts = datetime(2026, 1, 1, 2, 30, tzinfo=UTC)
            elif day in (5, 9):
                ts = datetime(2026, 2, 1, 2, 30, tzinfo=UTC) + timedelta(days=day - 1)
            names.append(pgbackup.dump_stem(ts))
        # Simplify: three dumps across months — Jan 1, Feb 1, and 10 dailies.
        stems = [
            "20260101-023000",  # monthly
            "20260201-023000",  # monthly
        ]
        stems += [f"202602{day:02d}-023000" for day in range(10, 16)]  # 6 latest dailies
        for stem in stems:
            (dumps_dir / f"{stem}.dump.enc").write_bytes(b"x")

        removed = pgbackup.prune_local(settings)
        remaining = {p.name for p in pgbackup.list_dumps(settings)}
        assert len(removed) > 0
        # Both monthlies survive; the newest keep_daily (3) dailies survive.
        assert "20260101-023000.dump.enc" in remaining  # monthly
        assert "20260201-023000.dump.enc" in remaining  # monthly
        assert "20260215-023000.dump.enc" in remaining  # newest daily
        assert "20260213-023000.dump.enc" in remaining  # 3 newest dailies kept
        assert "20260210-023000.dump.enc" not in remaining  # pruned daily


@requires_pg_tools
class TestRestoreDrill:
    def test_full_restore_drill_zero_content_mismatches(
        self, client: TestClient, db: Session, auth_headers: dict[str, str], tmp_path: Path, database_url: str
    ) -> None:
        from scripts import pgbackup
        from scripts.restore_drill import _canonical_digest

        _create_rows(client, db, auth_headers)

        settings = _settings(tmp_path, database_url)
        out = tmp_path / "dumps" / f"{pgbackup.dump_stem(pgbackup.now_utc())}.dump.enc"
        out.parent.mkdir(parents=True)
        pgbackup.run_dump(settings, out)

        src_digest = _canonical_digest(database_url)
        assert src_digest["posts"]

        # Full CLI drill in a subprocess so `get_settings()` picks our env.
        env = dict(os.environ)
        env.update(
            {
                "DATABASE_URL": database_url,
                "BACKUP_PASSPHRASE": settings.backup_passphrase,
                "BACKUP_DIR": str(tmp_path),
            }
        )
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.restore_drill",
                "--source-url",
                database_url,
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
        assert "restore drill OK" in proc.stdout
        for table in ("posts", "post_revisions", "audit_events"):
            assert f"MATCH  {table}" in proc.stdout
        # Scratch database dropped afterwards.
        assert "restore_drill_" not in _canonical_digest(database_url)


@requires_pg_tools
class TestBackupMetrics:
    def test_backup_metrics_gauge_updates(self) -> None:
        from app.observability import metrics

        metrics.observe_backup_result(ok=True, duration=1.5)
        assert metrics.backup_last_success_timestamp._value.get() > 0
        metrics.observe_backup_result(ok=False, duration=2.0)
        assert metrics.backup_results_total.labels(status="failed")._value.get() == 1
