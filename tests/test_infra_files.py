"""Dev-loop plumbing (#2): Makefile targets, compose files, Dockerfile, CI."""

from __future__ import annotations

import os
import re
import subprocess
import sys
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


def test_selfhost_e2e_asserts_the_human_dashboard() -> None:
    """#42 shipped a dashboard that 404'd in every deployment because nothing
    asserted the human surface. The deploy proof must load it (#37)."""
    script = REPO_ROOT / "scripts" / "selfhost_e2e.sh"
    body = script.read_text()
    check = 'wait_for_http "$BASE_URL/dashboard/login" "GET /dashboard/login"'
    assert check in body, (
        "scripts/selfhost_e2e.sh must assert the dashboard login page with a real"
        " status check (not a comment)"
    )


def test_deploy_smoke_token_can_create_a_site() -> None:
    """#45: the smoke token must carry `sites:write`, or `make selfhost-verify`
    fails on the very first API call a self-hoster makes."""
    body = (REPO_ROOT / "scripts" / "deploy_smoke.sh").read_text()
    assert '"sites:write"' in body


REPO = Path(__file__).resolve().parents[1]


def test_selfhost_e2e_asserts_the_embed_surface_with_a_read_only_token() -> None:
    """#37: /embed/v1/posts refuses write tokens, so the seed must issue a read-only one.

    Every layer is asserted, because each one was missing: the seed minted only the
    write token, and the smoke script then asked the embed feed for posts with it.
    """
    seed = (REPO / "scripts" / "seed.py").read_text()
    assert "DEMO_EMBED_LINK_LABEL" in seed
    assert 'embed_link.verbs = ["posts:read"]' in seed, "the embed link must be read-only"
    assert "Embed token (read-only)" in seed, "a self-hoster must be able to find it"

    e2e = (REPO / "scripts" / "selfhost_e2e.sh").read_text()
    assert "SMOKE_EMBED_TOKEN" in e2e, "the E2E must hand the read-only token to the smoke script"

    smoke = (REPO / "scripts" / "deploy_smoke.sh").read_text()
    assert "token=${EMBED_TOKEN}" in smoke, "the embed feed must be read with the read-only token"
    assert "token=${CAP_TOKEN}&limit=5" in smoke, "and the write token must be asserted as refused"


def test_deploy_smoke_never_asserts_a_get_endpoint_with_head() -> None:
    """#37: `curl -I` sends HEAD, FastAPI has no HEAD route, so the check read a 405 body."""
    smoke = (REPO / "scripts" / "deploy_smoke.sh").read_text()
    offenders = [ln.strip() for ln in smoke.splitlines() if re.search(r"(^|\s)-I(\s|$)", ln)]
    assert not offenders, f"HEAD assertions read error bodies as verdicts: {offenders}"


def test_caddyfile_only_uses_matchers_it_can_adapt() -> None:
    """#37: the production Caddyfile could not adapt in any mode -- keep it boring.

    Checked structurally over non-comment lines, so it runs without Docker (the
    docker-gated `caddy validate` test covers actual adaptation).
    """
    raw = (REPO / "deploy" / "compose" / "Caddyfile").read_text().splitlines()
    lines = [ln.strip() for ln in raw if ln.strip() and not ln.strip().startswith("#")]
    body = "\n".join(lines)

    assert "{$DOMAIN != " not in body, "no env-var comparison: Caddy cannot adapt that"
    assert "{$DOMAIN:localhost}" in body, "the domain needs a default so a bare checkout adapts"
    assert body.count("@embed_preflight") == 2, "the preflight matcher must be declared once"
    assert "header Origin {$EMBED_ORIGINS}" not in body, "set a literal header, not a bare env var"

    matchers = [ln for ln in lines if ln.startswith("@") and not ln.endswith("{")]
    assert matchers, "expected named matchers in the production Caddyfile"
    for ln in matchers:
        assert len(ln.split()) > 1, f"matcher without a value: {ln}"


CURL_STUB = r"""#!/usr/bin/env bash
# A curl stand-in for the deploy_smoke.sh regression test below: it answers every
# URL the smoke script probes and never sends an access-control-allow-origin
# header -- the documented deny-all default (`EMBED_ORIGINS=` in
# deploy/.env.example).  `-w %{http_code}` callers get a status code, `-D`
# callers get response headers, everyone else gets a body.
set -euo pipefail
args="$*"
code="200"
body=""
headers=$'HTTP/1.1 200 OK\r\ncontent-type: application/javascript\r\n\r\n'
case "$args" in
    *"/embed/v1/posts"*)
        if [[ "$args" == *"%{http_code}"* ]]; then
            code="403"   # write/admin tokens must be refused on the embed surface
        else
            body='{"posts":[{"title":"Smoke Test Post"}],"total":1}'
        fi
        ;;
    *"/embed/v1/agentcms.js"*) body="// agentcms-embed script" ;;
    *"/posts.json"*)           body='{"items":[{"slug":"smoke-test-post","title":"Smoke Test Post"}]}' ;;
    *"/v1/admin/tokens"*)      body='{"token":"acms_smoketoken"}' ;;
    *"/publish"*)              body='{"status":"published"}' ;;
    *"/c/"*)                   body='{"id":"11111111-1111-1111-1111-111111111111"}' ;;
    *"/healthz"* | *"/readyz"*) ;;
    *)                         body="<html><body><h1>Smoke Test Post</h1></body></html>" ;;
esac
if [[ "$args" == *"%{http_code}"* ]]; then
    printf '%s' "$code"
elif [[ "$args" == *"-D"* ]]; then
    printf '%s' "$headers"
else
    printf '%s' "$body"
fi
"""


def test_deploy_smoke_cors_check_cannot_abort_the_script(tmp_path: Path) -> None:
    """Regression (#44): a missing CORS header must not kill the smoke run.

    The CORS check read `$(echo ... | grep ... | head -1 | ...)` under
    `set -euo pipefail`.  With the documented default (`EMBED_ORIGINS=` in
    deploy/.env.example = deny-all) a preflight carries no
    `access-control-allow-origin`, `grep` exits 1, pipefail fails the whole
    substitution and bash -e ends the script *before* its own warn branch -- so
    the self-host E2E job went red on a healthy stack (2026-09-25, d54b2ec) and
    the operator saw "the API roundtrip failed" with no assertion behind it.
    """
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    curl = stub_bin / "curl"
    curl.write_text(CURL_STUB)
    curl.chmod(0o755)

    env = {**os.environ, "PATH": f"{stub_bin}{os.pathsep}{os.environ['PATH']}"}
    proc = _run(
        [
            str(REPO_ROOT / "scripts" / "deploy_smoke.sh"),
            "--base-url",
            "http://stub.invalid",
            "--compose-file",
            "deploy/compose/docker-compose.prod.yml",
            "--max-wait",
            "5",
            "--site-slug",
            "blog",
            "--capability-token",
            "cap_blog_ab-cd_ef",  # base64url: the token may contain '-' and '_'
            "--embed-token",
            "cap_blog_ro-xy",
        ],
        env=env,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ALL SMOKE TESTS PASSED" in proc.stdout, proc.stdout
    assert "CORS deny-all confirmed" in proc.stdout, proc.stdout


# --- the release pipeline itself (#38): a tag is the only thing that ships ---


def _release_workflow() -> str:
    return (REPO_ROOT / ".github" / "workflows" / "release.yml").read_text()


def _workflow_step_body(workflow: str, name: str) -> str:
    """Return the `run:` script of the named step, de-indented.

    The release workflow is read as text on purpose: parsing YAML here would
    need a dependency the fast job does not have, and the assertions below are
    about *what the step executes*, not about YAML structure.
    """
    lines = workflow.splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip() == f"- name: {name}"), None)
    assert start is not None, f"release.yml has no step named {name!r}"
    body: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("      - ") or (line and not line.startswith("  ")):
            break
        body.append(line)
    run = "\n".join(body)
    assert "run: |" in run, f"step {name!r} has no `run:` block"
    script = run.split("run: |", 1)[1].splitlines()
    indents = [len(line) - len(line.lstrip()) for line in script if line.strip()]
    indent = min(indents)
    return "\n".join(line[indent:] if line.strip() else "" for line in script).strip()


def test_release_workflow_fires_on_a_version_tag_only() -> None:
    """A tag publishes an image (and can be re-issued); a branch push must not."""
    workflow = _release_workflow()
    assert 'tags: ["v*"]' in workflow, "the image must be cut by a version tag (#38)"
    assert "workflow_dispatch" in workflow, "a fix to the publish job must be re-runnable"
    assert "workflow_dispatch:" in workflow.split("jobs:", 1)[0], (
        "workflow_dispatch must be a trigger, not just a mention"
    )
    assert "branches:" not in workflow.split("jobs:", 1)[0], (
        "every push to main would publish an image; only tags may"
    )
    assert 'case "$ref" in' in workflow and "v[0-9]*)" in workflow


def test_release_workflow_rejects_a_tag_that_disagrees_with_pyproject(tmp_path: Path) -> None:
    """The version guard is executed here, not merely asserted on (#38).

    A tag that disagrees with the package metadata ships an image that lies about
    what it is, so the guard is the load-bearing line of the release workflow —
    and a regression test that only greps for it would not notice it being
    weakened (`[ "$version" = "0.3.1" ]`, say).
    """
    guard = _workflow_step_body(_release_workflow(), "The tag must match pyproject.toml")
    ref_expr = "${{ steps.tag.outputs.ref }}"
    assert ref_expr in guard, "the guard must compare the tag that was pushed"
    assert "pyproject.toml" in guard and "exit 1" in guard

    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "agentcms"\nversion = "0.3.1"\n', encoding="utf-8"
    )
    # The guard shells out to `python3`, which needs 3.11+ for `tomllib`; the
    # runner provides that, a laptop's /usr/bin/python3 may not.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "python3").symlink_to(sys.executable)
    env = {
        **os.environ,
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
        "GITHUB_OUTPUT": str(tmp_path / "out.txt"),
    }
    for ref, ok in (("v0.3.1", True), ("v0.3.0", False), ("0.3.1", False), ("v0.4.0", False)):
        proc = subprocess.run(
            ["bash", "-e", "-c", guard.replace(ref_expr, ref)],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert (proc.returncode == 0) is ok, (
            f"tag {ref!r}: expected {'success' if ok else 'failure'}, got {proc.returncode}\n"
            f"{proc.stdout}{proc.stderr}"
        )
        if not ok:
            assert "pyproject.toml" in proc.stderr, proc.stderr


def test_release_workflow_boots_the_published_digest_with_no_skip_hatch() -> None:
    """The published image is deployed and exercised, or the run is red (#38)."""
    workflow = _release_workflow()
    assert "  verify-published:" in workflow, "pushing is not shipping: something must boot the digest"
    verify = workflow.split("  verify-published:", 1)[1]
    assert "needs: publish" in verify, "a failed publish must not leave a green run"
    assert 'docker pull "$PINNED"' in verify, "the job must pull what it published"
    assert "ghcr.io/${{ needs.publish.outputs.image }}@${{ needs.publish.outputs.digest }}" in verify, (
        "the boot must be pinned to the digest, not to a moving tag"
    )
    assert "--no-build" in verify, "the published image is booted, not rebuilt"
    assert "selfhost_e2e.sh" in verify, "one driver: the same script CI runs on the source build"
    for hatch in ("continue-on-error", "docker-available", "if: false"):
        assert hatch not in workflow, f"{hatch!r} would let a broken release go green"
