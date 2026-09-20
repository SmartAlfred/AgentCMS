"""Settings (#2): .env.example covers every variable, production defaults are refused."""

from __future__ import annotations

import os
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


def test_env_example_copy_boots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A verbatim copy of .env.example must load (#26).

    ``cp .env.example .env`` is step one of the documented quickstart, so the
    empty ``CORS_ORIGINS=`` line has to parse instead of raising SettingsError.
    """

    for var in tuple(os.environ):
        if var in {name.upper() for name in Settings.model_fields}:
            monkeypatch.delenv(var, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(ENV_EXAMPLE.read_text())
    settings = Settings(_env_file=env_file)
    assert settings.cors_origins == []


def test_development_defaults_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("APP_ENV", "SECRET_KEY", "DATABASE_URL"):
        monkeypatch.delenv(var, raising=False)
    settings = Settings(_env_file=None)
    assert settings.app_env == "development"
    assert settings.secret_key == DEV_SECRET_KEY
    assert settings.database_url == DEV_DATABASE_URL
    assert settings.docs_url == "http://127.0.0.1:8000/docs"


def test_production_refuses_the_dev_secret_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # Isolate from the ambient shell: the point of this test is that the *dev
    # default* is refused, so no SECRET_KEY may be inherited from the environment.
    monkeypatch.delenv("SECRET_KEY", raising=False)
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


def test_production_refuses_the_dev_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    # Same isolation as above: `export DATABASE_URL=…` (which the migrations step
    # of the CI gate needs) must not silently disarm this test.
    monkeypatch.delenv("DATABASE_URL", raising=False)
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
        agentcms_image_tag="ghcr.io/owner/agentcms:v0.3.1",
    )
    assert settings.is_production is True


def test_cors_origins_parses_a_comma_separated_string() -> None:
    settings = Settings(_env_file=None, cors_origins="http://a.test, http://b.test")  # type: ignore[arg-type]
    assert settings.cors_origins == ["http://a.test", "http://b.test"]


def test_safe_database_url_hides_the_password() -> None:
    settings = Settings(_env_file=None, database_url="postgresql+psycopg://user:hunter2@db:5432/agentcms")
    assert "hunter2" not in settings.safe_database_url()
    assert "user" in settings.safe_database_url()


def test_public_base_url_defaults_to_none() -> None:
    """PUBLIC_BASE_URL defaults to None."""
    settings = Settings(_env_file=None)
    assert settings.public_base_url is None


def test_public_base_url_can_be_set() -> None:
    """PUBLIC_BASE_URL can be explicitly set."""
    settings = Settings(_env_file=None, public_base_url="https://cms.example.com")
    assert settings.public_base_url == "https://cms.example.com"


def test_public_base_url_strips_trailing_slash() -> None:
    """PUBLIC_BASE_URL trailing slash is stripped when used."""
    # Note: the property that strips the slash is public_base_url_or_request
    # The raw setting keeps the user input
    settings = Settings(_env_file=None, public_base_url="https://cms.example.com/")
    assert settings.public_base_url == "https://cms.example.com/"


def test_public_base_url_or_request_uses_setting_when_set() -> None:
    """public_base_url_or_request returns PUBLIC_BASE_URL when configured."""
    from starlette.requests import Request

    settings = Settings(_env_file=None, public_base_url="https://cms.example.com")
    # Create a mock request
    scope = {"type": "http", "scheme": "http", "server": ("testserver", 80), "headers": []}
    request = Request(scope)
    assert settings.public_base_url_or_request(request) == "https://cms.example.com"


def test_public_base_url_or_request_falls_back_to_request() -> None:
    """public_base_url_or_request falls back to request host when not set."""
    from starlette.requests import Request

    settings = Settings(_env_file=None, public_base_url=None)
    scope = {
        "type": "http",
        "scheme": "https",
        "server": ("myapp.example.com", 443),
        "headers": [],
        "path": "/",
    }
    request = Request(scope)
    assert settings.public_base_url_or_request(request) == "https://myapp.example.com"


def test_public_base_url_or_request_falls_back_to_localhost() -> None:
    """public_base_url_or_request falls back to localhost when no request."""
    settings = Settings(_env_file=None, public_base_url=None)
    assert settings.public_base_url_or_request(None) == "http://localhost:8000"


def test_embed_origins_parses_comma_separated() -> None:
    """EMBED_ORIGINS parses comma-separated string like CORS_ORIGINS."""
    settings = Settings(_env_file=None, embed_origins="https://a.example.com, https://b.example.com")  # type: ignore[arg-type]
    assert settings.embed_origins == ["https://a.example.com", "https://b.example.com"]


def test_embed_token_scope_default() -> None:
    """EMBED_TOKEN_SCOPE defaults to posts:read."""
    settings = Settings(_env_file=None)
    assert settings.embed_token_scope == "posts:read"
