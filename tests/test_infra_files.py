"""Dev-loop plumbing (#2): Makefile targets, compose files, Dockerfile, CI."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from tests.pg import skip_or_fail_without_docker

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
    "selfhost-e2e",
    "selfhost-verify",
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


def test_compose_files_validate() -> None:
    skip_or_fail_without_docker("compose file validation")
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
    assert "schedule:" in workflow  # nightly run
    assert "postgres:16" in workflow
    assert "ruff check" in workflow
    assert "mypy" in workflow
    assert "pytest" in workflow
    assert "selfhost:" in workflow  # self-host deploy job


def test_deploy_compose_validates_with_env_file() -> None:
    """Regression test for #35: the documented deploy/compose/docker-compose.prod.yml
    must parse when the repository-root .env is passed explicitly via --env-file.
    """
    skip_or_fail_without_docker("deploy compose validation")

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


# --- self-host E2E (#37) and the release it pins (#38) ----------------------


def test_selfhost_e2e_script_runs_the_documented_path_and_cannot_skip() -> None:
    """The CI verdict is only as good as the script behind it (#37)."""
    script = REPO_ROOT / "scripts" / "selfhost_e2e.sh"
    assert script.exists(), "scripts/selfhost_e2e.sh backs the #37 self-host E2E job"
    assert os.access(script, os.X_OK), "scripts/selfhost_e2e.sh must be executable"
    body = script.read_text()
    for needle in (
        "docker info",  # does the daemon actually answer?
        "not a skip",  # ... and does its absence fail the run?
        "deploy/compose/docker-compose.prod.yml",
        "--env-file",  # #35: the repository-root .env is not read implicitly
        "up -d --build",  # the documented command, verbatim
        "run --rm migrate",  # migrations must be re-runnable, not just applied once
        "/healthz",
        "/readyz",
        "RestartCount",  # a crash-looping container is not a healthy deploy
        "deploy_smoke.sh",  # create site -> publish post -> fetch the public URL
        "down -v",  # leaves no volumes behind
        "trap cleanup EXIT",
    ):
        assert needle in body, f"scripts/selfhost_e2e.sh must reference {needle!r}"

    # Join shell line-continuations so a guard's `|| die` sits on the same logical line.
    joined = body.replace("\\\n", " ")
    guards = [ln for ln in joined.splitlines() if "docker info" in ln or "command -v" in ln]
    assert guards, "the script must probe for docker and curl"
    for line in guards:
        assert "|| true" not in line and "|| :" not in line, (
            f"the precondition check must not be swallowed ({line.strip()!r}) — `|| true` "
            "would make this job green on a machine with no Docker, the exact failure mode "
            "#37 exists to stop"
        )
        assert re.search(r"\bdie\b", line), f"an unmet precondition must fail loudly: {line!r}"


def test_ci_runs_the_selfhost_e2e_job_without_an_escape_hatch() -> None:
    workflow = (REPO_ROOT / ".github/workflows/ci.yml").read_text()
    assert re.search(r"^  selfhost:$", workflow, re.M), "ci.yml has no selfhost job (#37)"
    job = workflow.split("\n  selfhost:\n", 1)[1]
    assert "./scripts/selfhost_e2e.sh" in job, "the selfhost job must run the repo's E2E script"
    assert "path: agentcms-clean" in job, "the stack must deploy from a fresh checkout directory"
    assert "continue-on-error" not in job, "a deploy check allowed to fail silently proves nothing"
    assert not re.search(r"^\s*if:.*(pull_request|always\(\))", job, re.M), (
        "the self-host check must run on pull requests too (nightly-only found #36 too late)"
    )
    assert "AGENTCMS_REQUIRE_DOCKER" in workflow, "CI must turn Docker-dependent skips into failures (#37)"


def test_release_workflow_publishes_a_pinnable_image() -> None:
    """#38: a tag produces a versioned image plus a digest worth pinning."""
    path = REPO_ROOT / ".github/workflows/release.yml"
    assert path.exists(), "no release workflow: self-hosters have nothing to pin (#38)"
    workflow = path.read_text()
    for needle in (
        'tags: ["v*"]',
        "packages: write",
        "contents: write",
        "tr '[:upper:]' '[:lower:]'",  # GHCR rejects the repo's uppercase name (#37)
        "deploy/docker/Dockerfile",  # the image the self-host path builds
        "pyproject.toml",  # the tag must match the package version
        "platforms: linux/amd64,linux/arm64",
        "GITHUB_STEP_SUMMARY",  # the digest is recorded, not only printed
        "gh release edit",  # ... and lands in the release notes
        "selfhost_e2e.sh",  # the published digest is booted, not merely pushed
    ):
        assert needle in workflow, f"release.yml must reference {needle!r}"

    # One lowercased image name everywhere: buildx aborted with
    # `invalid tag "ghcr.io/SmartAlfred/AgentCMS:v0.3.0": repository name must be
    # lowercase`, so the name is normalised once and the verify job reuses it.
    assert "ghcr.io/${{ steps.norm.outputs.image }}:" in workflow
    assert "env.IMAGE" not in workflow


def test_documented_commands_actually_exist() -> None:
    """Every `make x` / `./scripts/x.sh` in the docs must exist (#35, #37)."""
    makefile = (REPO_ROOT / "Makefile").read_text()
    targets = set(re.findall(r"^([a-zA-Z][\w-]*):", makefile, re.M))
    stopwords = {
        "sure",
        "it",
        "the",
        "a",
        "an",
        "and",
        "this",
        "that",
        "them",
        "your",
        "our",
        "one",
        "no",
        "sense",
        "sure,",
        "its",
        "their",
        "you",
        "us",
        "them,",
        "sure.",
    }
    docs = [
        REPO_ROOT / "README.md",
        *(REPO_ROOT / "docs").rglob("*.md"),
        *(REPO_ROOT / "deploy").rglob("*.md"),
    ]
    problems: list[str] = []
    for doc in docs:
        text = doc.read_text()
        rel = doc.relative_to(REPO_ROOT)
        if "--build --build" in text:
            problems.append(f"{rel}: duplicated --build flag")
        for target in re.findall(r"\bmake ([a-zA-Z][\w-]*)", text):
            if target not in targets and target not in stopwords:
                problems.append(f"{rel}: `make {target}` — no such target")
        for script in re.findall(r"\./(scripts/[\w./-]+\.sh)", text):
            if not (REPO_ROOT / script).exists():
                problems.append(f"{rel}: `./{script}` does not exist")
    assert not problems, "documented commands that do not exist: " + "; ".join(sorted(set(problems)))


def test_smoke_scripts_read_the_token_field_the_api_returns() -> None:
    """The plaintext a token creation returns is exposed as ``token``.

    ``TokenCreateResponse`` has no ``plaintext`` field, but both smoke scripts
    parsed ``.plaintext`` -- so the documented verify path could never mint a token
    and died with "Failed to create admin token: {.."token"..}" against a 201. The
    self-host E2E job caught it on its first CI run (#37).
    """
    from app.api.v1.admin_tokens import TokenCreateResponse

    fields = set(TokenCreateResponse.model_fields)
    assert "token" in fields, f"TokenCreateResponse dropped `token`: {sorted(fields)}"
    assert "plaintext" not in fields, sorted(fields)

    for script in ("scripts/deploy_smoke.sh", "scripts/upgrade_smoke.sh"):
        text = (REPO_ROOT / script).read_text()
        assert "jq -r '.plaintext" not in text, f"{script} parses a field the API never returns"
        assert "jq -r '.token // empty'" in text, f"{script} does not parse TokenCreateResponse.token"
