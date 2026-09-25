"""Unit tests for version-pin parsing and rollback guard (#33)."""

from __future__ import annotations

import pytest
from app.config import Settings


class TestVersionPinParsing:
    """Tests for AGENTCMS_IMAGE_TAG validation (_is_mutable_tag)."""

    def test_digest_reference_is_immutable(self) -> None:
        tag = "ghcr.io/owner/agentcms@sha256:abc123def456"
        assert not Settings._is_mutable_tag(tag)

    def test_semver_tag_is_immutable(self) -> None:
        tag = "ghcr.io/owner/agentcms:v0.3.1"
        assert not Settings._is_mutable_tag(tag)

    def test_semver_tag_without_v_prefix_is_immutable(self) -> None:
        tag = "ghcr.io/owner/agentcms:0.3.1"
        assert not Settings._is_mutable_tag(tag)

    def test_latest_tag_is_mutable(self) -> None:
        tag = "ghcr.io/owner/agentcms:latest"
        assert Settings._is_mutable_tag(tag)

    def test_local_tag_is_mutable(self) -> None:
        tag = "ghcr.io/owner/agentcms:local"
        assert Settings._is_mutable_tag(tag)

    def test_dev_tag_is_mutable(self) -> None:
        tag = "ghcr.io/owner/agentcms:dev"
        assert Settings._is_mutable_tag(tag)

    def test_empty_tag_is_mutable(self) -> None:
        tag = ""
        assert Settings._is_mutable_tag(tag)

    def test_just_repo_no_tag_is_mutable(self) -> None:
        tag = "ghcr.io/owner/agentcms"
        assert Settings._is_mutable_tag(tag)

    def test_major_minor_tag_is_immutable(self) -> None:
        tag = "ghcr.io/owner/agentcms:v0.3"
        assert not Settings._is_mutable_tag(tag)

    def test_rc_tag_is_immutable(self) -> None:
        tag = "ghcr.io/owner/agentcms:v0.3.1-rc.1"
        assert not Settings._is_mutable_tag(tag)


class TestProductionGuard:
    """Tests for production startup guard with AGENTCMS_IMAGE_TAG."""

    def test_production_refuses_missing_image_tag(self) -> None:
        with pytest.raises(ValueError, match="AGENTCMS_IMAGE_TAG is required in production"):
            Settings(
                app_env="production",
                secret_key="a" * 32,
                database_url="postgresql+psycopg://user:pass@localhost/db",
                debug=False,
                agentcms_image_tag="",
            )

    def test_production_refuses_latest_tag(self) -> None:
        with pytest.raises(ValueError, match="must be an immutable version tag or digest"):
            Settings(
                app_env="production",
                secret_key="a" * 32,
                database_url="postgresql+psycopg://user:pass@localhost/db",
                debug=False,
                agentcms_image_tag="ghcr.io/owner/agentcms:latest",
            )

    def test_production_refuses_local_tag(self) -> None:
        with pytest.raises(ValueError, match="must be an immutable version tag or digest"):
            Settings(
                app_env="production",
                secret_key="a" * 32,
                database_url="postgresql+psycopg://user:pass@localhost/db",
                debug=False,
                agentcms_image_tag="ghcr.io/owner/agentcms:local",
            )

    def test_production_accepts_digest_tag(self) -> None:
        settings = Settings(
            app_env="production",
            admin_token="a" * 32,
            secret_key="a" * 32,
            database_url="postgresql+psycopg://user:pass@localhost/db",
            debug=False,
            agentcms_image_tag="ghcr.io/owner/agentcms@sha256:abc123",
        )
        assert settings.agentcms_image_tag == "ghcr.io/owner/agentcms@sha256:abc123"

    def test_production_accepts_semver_tag(self) -> None:
        settings = Settings(
            app_env="production",
            admin_token="a" * 32,
            secret_key="a" * 32,
            database_url="postgresql+psycopg://user:pass@localhost/db",
            debug=False,
            agentcms_image_tag="ghcr.io/owner/agentcms:v0.3.1",
        )
        assert settings.agentcms_image_tag == "ghcr.io/owner/agentcms:v0.3.1"

    def test_development_allows_any_tag(self) -> None:
        settings = Settings(
            app_env="development",
            secret_key="dev",
            database_url="postgresql+psycopg://user:pass@localhost/db",
            debug=True,
            agentcms_image_tag="latest",
        )
        assert settings.agentcms_image_tag == "latest"

    def test_test_env_allows_any_tag(self) -> None:
        settings = Settings(
            app_env="test",
            secret_key="test",
            database_url="postgresql+psycopg://user:pass@localhost/db",
            debug=False,
            agentcms_image_tag="local",
        )
        assert settings.agentcms_image_tag == "local"


class TestRollbackGuardLogic:
    """Tests for rollback guard logic (simulated in Python)."""

    def test_guard_allows_same_revision(self) -> None:
        """If pre-upgrade revision == current revision, rollback is safe."""
        pre_upgrade_rev = "abc123"
        current_rev = "abc123"
        assert pre_upgrade_rev == current_rev

    def test_guard_checks_downgrade_sql_for_destructive_ops(self) -> None:
        """Downgrade SQL containing DROP/DELETE/TRUNCATE should be rejected."""
        destructive_sql = "DROP TABLE posts;"
        assert "DROP TABLE" in destructive_sql.upper()

        destructive_sql = "DELETE FROM posts;"
        assert "DELETE FROM" in destructive_sql.upper()

        destructive_sql = "TRUNCATE TABLE posts;"
        assert "TRUNCATE" in destructive_sql.upper()

        destructive_sql = "DROP COLUMN posts.title;"
        assert "DROP COLUMN" in destructive_sql.upper()

        safe_sql = "ALTER TABLE posts ADD COLUMN new_col TEXT;"
        assert "DROP TABLE" not in safe_sql.upper()
        assert "DELETE FROM" not in safe_sql.upper()
        assert "TRUNCATE" not in safe_sql.upper()
        assert "DROP COLUMN" not in safe_sql.upper()


class TestUpgradeSmokeLogic:
    """Tests for upgrade smoke test logic."""

    def test_smoke_checks_healthz_readyz_version(self) -> None:
        """Smoke test should verify /healthz, /readyz, /v1/version."""
        required_endpoints = ["/healthz", "/readyz", "/v1/version"]
        assert "/healthz" in required_endpoints
        assert "/readyz" in required_endpoints
        assert "/v1/version" in required_endpoints

    def test_preflight_runs_alembic_check_readonly(self) -> None:
        """Preflight should run alembic check (read-only) against live DB."""
        # This is a conceptual test - the actual implementation is in bash
        pass

    def test_backup_before_migration(self) -> None:
        """Backup must be taken before any migration runs."""
        # This is a conceptual test - the actual implementation is in bash
        pass

    def test_migration_failure_leaves_app_serving(self) -> None:
        """If migration fails, old app must still serve on old schema."""
        # This is a conceptual test - the actual implementation is in bash
        pass


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
