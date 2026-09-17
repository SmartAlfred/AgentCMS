"""Application settings (#2).

Every environment variable the service reads is declared on :class:`Settings`
and documented in ``.env.example``.  Two rules make misconfiguration loud:

* in ``APP_ENV=production`` the service refuses to boot while a
  development-only default is still in place (secret key, database URL);
* in ``APP_ENV=production`` the service refuses to boot with ``DEBUG=true``.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

DevSecretKey = str

DEV_SECRET_KEY = "dev-insecure-secret-change-me-please-000000000000"
DEV_DATABASE_URL = "postgresql+psycopg://agentcms:agentcms@localhost:5432/agentcms"

AppEnv = Literal["development", "test", "production"]
PublishMode = Literal["auto", "require_review"]


class Settings(BaseSettings):
    """Runtime configuration, read from the environment and ``.env``."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # -- runtime ------------------------------------------------------------
    app_env: AppEnv = "development"
    app_name: str = "AgentCMS"
    app_version: str = "0.3.0"
    log_level: str = "INFO"
    debug: bool = False
    docs_enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8000
    # ``NoDecode``: pydantic-settings would otherwise JSON-decode this complex
    # field before validation, and the documented empty value (``CORS_ORIGINS=``
    # in .env.example) is not valid JSON.  The validator below does the splitting.
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # -- security -----------------------------------------------------------
    secret_key: str = DEV_SECRET_KEY

    # -- database -----------------------------------------------------------
    database_url: str = DEV_DATABASE_URL
    db_pool_size: int = 5
    db_pool_timeout: int = 5
    db_echo: bool = False

    # -- object storage (S3-compatible; used from #20 onwards) --------------
    s3_endpoint_url: str | None = None
    s3_access_key_id: str | None = None
    s3_secret_access_key: str | None = None
    s3_bucket: str = "agentcms-media"
    s3_region: str = "us-east-1"
    s3_public_base_url: str | None = None

    # -- content defaults ---------------------------------------------------
    default_site_slug: str = "blog"
    default_publish_mode: PublishMode = "auto"
    idempotency_retention_hours: int = 24

    # -- capability links (#6) -----------------------------------------------
    capability_default_ttl_minutes: int = 60
    capability_max_ttl_days: int = 30
    capability_rate_limit_per_link: int = 30
    capability_rate_limit_window_seconds: int = 60
    capability_max_live_links_per_site: int = 100

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @model_validator(mode="after")
    def _guard_production(self) -> Settings:
        if self.app_env != "production":
            return self
        problems: list[str] = []
        if self.secret_key in {"", DEV_SECRET_KEY}:
            problems.append(
                "SECRET_KEY is unset or still the development value; "
                'generate one with python -c "import secrets; print(secrets.token_urlsafe(48))"'
            )
        elif len(self.secret_key) < 32:
            problems.append("SECRET_KEY must be at least 32 characters")
        if self.database_url == DEV_DATABASE_URL:
            problems.append("DATABASE_URL is still the development default")
        if self.debug:
            problems.append("DEBUG must be false when APP_ENV=production")
        if problems:
            raise ValueError("refusing to start in production: " + "; ".join(problems))
        return self

    # -- derived ------------------------------------------------------------
    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def is_test(self) -> bool:
        return self.app_env == "test"

    @property
    def docs_url(self) -> str:
        return f"http://{self.host}:{self.port}/docs"

    def safe_database_url(self) -> str:
        """Database URL with the password masked, safe for logs."""

        from sqlalchemy.engine import make_url

        return make_url(self.database_url).render_as_string(hide_password=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton."""

    return Settings()


def reset_settings_cache() -> None:
    """Drop the cached settings (used by tests)."""

    get_settings.cache_clear()
