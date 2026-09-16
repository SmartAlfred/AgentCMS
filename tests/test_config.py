"""Settings (#2): .env.example covers every variable, production defaults are refused."""

from __future__ import annotations

from pathlib import Path

import pytest
from app.config import DEV_DATABASE_URL, DEV_SECRET_KEY, Settings
from pydantic import ValidationError

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_EXAMPLE = REPO_ROOT / ".env.example"


def _documented_variables() -> set[str]:
    variables: set[str] = set()
    for line in ENV_EXAMPLE.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        variables.add(stripped.split("=", 1)[0].strip())
    return variables


def test_env_example_documents_every_setting() -> None:
    missing = {name.upper() for name in Settings.model_fields} - _documented_variables()
    assert not missing, f".env.example is missing: {sorted(missing)}"


def test_env_example_has_no_undocumented_setting() -> None:
    known = {name.upper() for name in Settings.model_fields}
    extra = {name for name in _documented_variables() if name not in known}
    # POSTGRES_* are consumed by compose.yml, not by Python; everything else must exist.
    allowed_compose_only = {"POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB", "POSTGRES_PORT"}
    assert extra <= allowed_compose_only, (
        f".env.example documents unknown variables: {sorted(extra - allowed_compose_only)}"
    )


def test_development_defaults_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("APP_ENV", "SECRET_KEY", "DATABASE_URL"):
        monkeypatch.delenv(var, raising=False)
    settings = Settings(_env_file=None)
    assert settings.app_env == "development"
    assert settings.secret_key == DEV_SECRET_KEY
    assert settings.database_url == DEV_DATABASE_URL
    assert settings.docs_url == "http://127.0.0.1:8000/docs"


def test_production_refuses_the_dev_secret_key() -> None:
    with pytest.raises(ValidationError) as excinfo:
        Settings(
            _env_file=None,
            app_env="production",
            database_url="postgresql+psycopg://u:p@db:5432/agentcms",
        )
    assert "SECRET_KEY" in str(excinfo.value)


def test_production_refuses_a_short_secret_key() -> None:
    with pytest.raises(ValidationError) as excinfo:
        Settings(
            _env_file=None,
            app_env="production",
            secret_key="too-short",
            database_url="postgresql+psycopg://u:p@db:5432/agentcms",
        )
    assert "at least 32 characters" in str(excinfo.value)


def test_production_refuses_the_dev_database_url() -> None:
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None, app_env="production", secret_key="x" * 40)
    assert "DATABASE_URL" in str(excinfo.value)


def test_production_refuses_debug() -> None:
    with pytest.raises(ValidationError) as excinfo:
        Settings(
            _env_file=None,
            app_env="production",
            secret_key="x" * 40,
            database_url="postgresql+psycopg://u:p@db:5432/agentcms",
            debug=True,
        )
    assert "DEBUG" in str(excinfo.value)


def test_production_accepts_explicit_configuration() -> None:
    settings = Settings(
        _env_file=None,
        app_env="production",
        secret_key="x" * 40,
        database_url="postgresql+psycopg://u:p@db:5432/agentcms",
    )
    assert settings.is_production is True


def test_cors_origins_parses_a_comma_separated_string() -> None:
    settings = Settings(_env_file=None, cors_origins="http://a.test, http://b.test")  # type: ignore[arg-type]
    assert settings.cors_origins == ["http://a.test", "http://b.test"]


def test_safe_database_url_hides_the_password() -> None:
    settings = Settings(_env_file=None, database_url="postgresql+psycopg://user:hunter2@db:5432/agentcms")
    assert "hunter2" not in settings.safe_database_url()
    assert "user" in settings.safe_database_url()
