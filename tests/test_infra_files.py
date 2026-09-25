"""Dev-loop plumbing (#2): Makefile targets, compose files, Dockerfile, CI."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

REQUIRED_MAKE_TARGETS = (
    "dev",
    "test",
    "lint",
    "migrate",
    "seed",
    "up",
    "down",
    "docker-build",
    "selfhost",
)


def _run(cmd: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, check=False, env=env)


def test_makefile_declares_the_documented_targets() -> None:
    makefile = (REPO_ROOT / "Makefile").read_text()
    for target in REQUIRED_MAKE_TARGETS:
        assert f"\n{target}:" in makefile, f"Makefile has no `{target}` target"


def test_make_help_lists_every_target() -> None:
    result = _run(["make", "help"])
    assert result.returncode == 0, result.stderr
    for target in REQUIRED_MAKE_TARGETS:
        assert target in result.stdout


def test_dockerfile_is_multi_stage_and_runs_unprivileged() -> None:
    dockerfile = (REPO_ROOT / "Dockerfile").read_text()
    assert "FROM python:3.11-slim AS build" in dockerfile
    assert "FROM python:3.11-slim AS runtime" in dockerfile
    assert "USER agentcms" in dockerfile
    assert "HEALTHCHECK" in dockerfile


def test_prod_compose_serves_api_and_db() -> None:
    compose = (REPO_ROOT / "compose.prod.yml").read_text()
    assert "api:" in compose
    assert "db:" in compose
    assert "build:" in compose


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not installed")
def test_compose_files_validate() -> None:
    if _run(["docker", "info"]).returncode != 0:
        pytest.skip("docker daemon not running")
    env = dict(
        os.environ,
        POSTGRES_PASSWORD="ci-password",
        SECRET_KEY="c" * 40,
    )
    dev = _run(["docker", "compose", "-f", "compose.yml", "config", "--quiet"], env=env)
    assert dev.returncode == 0, dev.stderr
    prod = _run(["docker", "compose", "-f", "compose.prod.yml", "config", "--quiet"], env=env)
    assert prod.returncode == 0, prod.stderr


def test_ci_workflow_runs_lint_and_tests_on_every_push() -> None:
    workflow = (REPO_ROOT / ".github/workflows/ci.yml").read_text()
    assert "on:" in workflow
    assert "push:" in workflow
    assert "postgres:16" in workflow
    assert "ruff check" in workflow
    assert "mypy" in workflow
    assert "pytest" in workflow


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not installed")
def test_deploy_compose_validates_with_env_file() -> None:
    """Regression test for #35: the documented deploy/compose/docker-compose.prod.yml
    must parse when the repository-root .env is passed explicitly via --env-file.
    """
    if _run(["docker", "info"]).returncode != 0:
        pytest.skip("docker daemon not running")

    # Create a minimal .env with required production variables
    env = dict(
        os.environ,
        POSTGRES_PASSWORD="ci-password",
        SECRET_KEY="c" * 40,
        AGENTCMS_IMAGE_TAG="agentcms:ci-test",
        DOMAIN="localhost",
    )

    # Write a temporary .env file to simulate the documented quickstart
    import tempfile

    with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
        f.write("POSTGRES_PASSWORD=ci-password\n")
        f.write("SECRET_KEY=" + "c" * 40 + "\n")
        f.write("AGENTCMS_IMAGE_TAG=agentcms:ci-test\n")
        f.write("DOMAIN=localhost\n")
        f.write("POSTGRES_USER=agentcms\n")
        f.write("POSTGRES_DB=agentcms\n")
        env_file_path = f.name

    try:
        result = _run(
            [
                "docker",
                "compose",
                "--env-file",
                env_file_path,
                "-f",
                "deploy/compose/docker-compose.prod.yml",
                "config",
                "--quiet",
            ],
            env=env,
        )
        assert result.returncode == 0, (
            f"deploy/compose/docker-compose.prod.yml failed to parse with --env-file: {result.stderr}"
        )
    finally:
        import os as _os

        _os.unlink(env_file_path)


def test_selfhost_script_generates_a_bootable_production_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`make selfhost` is the documented deploy path (#35): prove it writes an
    .env the app accepts with APP_ENV=production, without needing Docker."""
    from app.config import Settings

    script = REPO_ROOT / "scripts" / "selfhost.sh"
    assert script.exists(), "scripts/selfhost.sh backs the documented `make selfhost`"
    assert os.access(script, os.X_OK), "scripts/selfhost.sh must be executable"

    env = dict(os.environ)
    for name in (
        "APP_ENV",
        "DATABASE_URL",
        "SECRET_KEY",
        "AGENTCMS_IMAGE_TAG",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_DB",
    ):
        env.pop(name, None)

    env_file = tmp_path / "prod.env"
    cmd = ["bash", str(script), "--setup-only", "--env-file", str(env_file)]
    first = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, check=False, env=env)
    assert first.returncode == 0, first.stderr
    generated = env_file.read_text()
    assert "AGENTCMS_IMAGE_TAG=" in generated
    assert not re.search(r"^AGENTCMS_IMAGE_TAG=\s*$", generated, re.MULTILINE), (
        "a blank tag makes the app refuse to boot when APP_ENV=production"
    )

    # Clear APP_ENV from the current process env so Settings reads it from the .env file
    monkeypatch.delenv("APP_ENV", raising=False)
    settings = Settings(_env_file=env_file)
    assert settings.app_env == "production"
    assert settings.agentcms_image_tag
    assert len(settings.secret_key) >= 32
    assert settings.database_url != "postgresql+psycopg://agentcms:agentcms@localhost:5432/agentcms"

    second = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, check=False, env=env)
    assert second.returncode == 0, second.stderr
    assert env_file.read_text() == generated, "re-running must not rotate secrets"
