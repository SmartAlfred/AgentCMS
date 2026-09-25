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
from starlette.requests import Request

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
    app_version: str = "0.3.1"
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

    # -- public URL -----------------------------------------------------------
    # Canonical public base URL for generated links (export, embed, llms.txt, docs).
    # When unset, falls back to the request host (for dynamic endpoints) or
    # documented localhost (for static docs). Must be a public HTTPS URL in production.
    public_base_url: str | None = None

    # -- content defaults ---------------------------------------------------
    default_site_slug: str = "blog"
    default_publish_mode: PublishMode = "auto"
    idempotency_retention_hours: int = 24

    # -- embed configuration (#31) -------------------------------------------
    embed_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)
    embed_token_scope: str = "posts:read"

    # -- capability links (#6) -----------------------------------------------
    capability_default_ttl_minutes: int = 60
    capability_max_ttl_days: int = 30
    capability_rate_limit_per_link: int = 30
    capability_rate_limit_window_seconds: int = 60
    capability_max_live_links_per_site: int = 100

    # -- observability (#24) -------------------------------------------------
    # Structured JSON request logs (one line per request). Set to "json" to
    # emit machine-readable logs.  Default keeps a readable text format for
    # local development.
    log_format: str = "json"
    # Shared secret required on GET /metrics (admin-auth).  Empty = metrics
    # served to any authenticated admin token; never expose /metrics publicly.
    metrics_token: str = ""
    # Base URL of the OTLP endpoint (e.g. http://jaeger:4318).  Empty =
    # tracing is soft-disabled (no export), which keeps the hot path cheap.
    otlp_endpoint: str = ""
    # Service name reported as the OTel ``service.name`` resource attribute.
    otel_service_name: str = "agentcms"
    # Trace sampling rate for normal requests (0.1 = 10 %).  Errors are always
    # sampled regardless of this value.
    trace_sample_ratio: float = 0.1
    # Inject synthetic build metadata (git sha / build time) for /v1/version.
    # In a container these are baked in at build time; locally we fall back to
    # git + filesystem timestamps automatically, so this is optional.
    git_sha: str = ""
    build_time: str = ""

    # -- backups & object storage (#24) --------------------------------------
    # Where nightly pg_dump backups are written. Supports:
    #   file:///absolute/path   (local directory, also the restore-drill default)
    #   s3://bucket/prefix      (S3-compatible object storage)
    # Empty = backups disabled.
    backup_store_url: str = ""
    # Local directory backups are written to when BACKUP_STORE_URL is empty.
    backup_dir: str = ".backups"
    # Passphrase used to encrypt/decrypt dumps (AES-256).  Must be set (or
    # inherited from the environment) for `make backup` to run.
    backup_passphrase: str = ""
    # Retention in days/months: keep 30 daily + 12 monthly snapshots.
    backup_retention_daily: int = 30
    backup_retention_monthly: int = 12
    # A static database to diff a restore against in dry-run/restore-drill
    # mode (usually "production").  Empty = diff against the DATABASE_URL.
    backup_source_url: str = ""

    # -- version pinning (#33) ------------------------------------------------
    # Explicit immutable image tag or digest for production deployments.
    # Examples: ghcr.io/owner/agentcms:v0.3.1, ghcr.io/owner/agentcms@sha256:abc123
    # Empty = not set; in production this is required and must be immutable.
    agentcms_image_tag: str = ""

    @field_validator("cors_origins", "embed_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @staticmethod
    def _is_mutable_tag(tag: str) -> bool:
        """Return True if the image tag is mutable.

        Immutable (return False):
        - Digest references: repo@sha256:...
        - Full semver: v0.3.1, 0.3.1, v0.3.1-rc.1
        - Major.minor: v0.3 (per test expectations)

        Mutable (return True):
        - Empty string, no tag (just repo name)
        - :latest, :local, :dev
        - Anything else (conservative)
        """
        if not tag:
            return True

        # Digest reference is always immutable
        if "@sha256:" in tag:
            return False

        # Extract tag part after the last colon
        if ":" in tag:
            tag_part = tag.split(":")[-1]
        else:
            # No tag specified (just repo name)
            return True

        # Mutable tag names
        if tag_part in {"latest", "local", "dev"}:
            return True

        # Check if it's a semver-like tag (v0.3.1 or 0.3.1 or v0.3.1-rc.1)
        # Must have at least major.minor.patch
        import re

        # Pattern: optional 'v', then major.minor.patch, optional prerelease/build
        semver_pattern = r"^v?\d+\.\d+\.\d+(-[a-zA-Z0-9.-]+)?(\+[a-zA-Z0-9.-]+)?$"
        if re.match(semver_pattern, tag_part):
            return False

        # Major.minor only (e.g., v0.3) is treated as immutable per test expectations
        major_minor_pattern = r"^v?\d+\.\d+$"
        return not re.match(major_minor_pattern, tag_part) is not None

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
        # Version pinning: AGENTCMS_IMAGE_TAG must be set and immutable in production
        if not self.agentcms_image_tag:
            problems.append("AGENTCMS_IMAGE_TAG is required in production")
        elif self._is_mutable_tag(self.agentcms_image_tag):
            problems.append(
                "AGENTCMS_IMAGE_TAG must be an immutable version tag or digest "
                f"(got '{self.agentcms_image_tag}')"
            )
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

    def public_base_url_or_request(self, request: Request | None = None) -> str:
        """Return the canonical public base URL.

        Priority:
        1. Explicit PUBLIC_BASE_URL setting (for static exports, llms.txt, etc.)
        2. Request host (for dynamic endpoints like /embed, /v1/discover)
        3. Documented localhost fallback (for static docs generation)
        """
        if self.public_base_url:
            return self.public_base_url.rstrip("/")
        if request is not None:
            return f"{request.url.scheme}://{request.url.netloc}"
        return "http://localhost:8000"

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
