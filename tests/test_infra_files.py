"""Dev-loop plumbing (#2): Makefile targets, compose files, Dockerfile, CI."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
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
# A curl stand-in for the deploy_smoke.sh regression tests below: it answers every
# URL the smoke script probes and never sends an access-control-allow-origin
# header -- the documented deny-all default (`EMBED_ORIGINS=` in
# deploy/.env.example).  `-w %{http_code}` callers get a status code, `-D`
# callers get response headers, everyone else gets a body.
#
# STUB_REFLECT_ORIGIN=… makes the stub behave like an *edge that reflects an
# origin* (#48), so the smoke script's CORS assertion can be exercised in every
# direction rather than only the deny-all one.
#
# It models the #44 admin guard too: /v1/admin/tokens answers 401 without an
# X-Admin-Token header and 201 with it. STUB_ADMIN_ANON_CODE=201 simulates the
# guard going missing (or a loopback-exempt vantage) and STUB_ADMIN_MINT_CODE=401
# simulates a stack that refuses the correct secret -- the two ways the smoke
# script's new #44 assertion and its positive control must be exercised.
#
# `-o <file>` is honoured (real curl writes the body there and the code to
# stdout), so the smoke script can capture a response body *and* its status.
set -euo pipefail
args="$*"
code="200"
body=""
out_file=""
prev=""
for arg in "$@"; do
    [[ "$prev" == "-o" ]] && out_file="$arg"
    prev="$arg"
done
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
    *"/v1/admin/tokens"*)
        body='{"token":"acms_smoketoken"}'
        if [[ "$args" == *"%{http_code}"* ]]; then
            if [[ "$args" == *"X-Admin-Token: "* ]]; then
                code="${STUB_ADMIN_MINT_CODE:-201}"
            else
                code="${STUB_ADMIN_ANON_CODE:-401}"
            fi
        fi
        ;;
    *"/publish"*)              body='{"status":"published"}' ;;
    *"/c/"*)                   body='{"id":"11111111-1111-1111-1111-111111111111"}' ;;
    *"/healthz"* | *"/readyz"*) ;;
    *)                         body="<html><body><h1>Smoke Test Post</h1></body></html>" ;;
esac
if [[ -n "${STUB_REFLECT_ORIGIN:-}" ]] \
    && [[ "$args" == *"/embed/v1/posts"* ]] \
    && [[ "$args" == *"Origin: ${STUB_REFLECT_ORIGIN}"* ]]; then
    headers=$'HTTP/1.1 204 No Content\r\ncontent-type: text/plain\r\n'
    headers+="access-control-allow-origin: ${STUB_REFLECT_ORIGIN}"$'\r\n\r\n'
fi
if [[ "$args" == *"%{http_code}"* ]]; then
    [[ -z "$out_file" ]] || printf '%s' "$body" > "$out_file"
    printf '%s' "$code"
elif [[ "$args" == *"-D"* ]]; then
    printf '%s' "$headers"
else
    printf '%s' "$body"
fi
"""


ADMIN_BOOTSTRAP_SECRET = "bootstrap-admin-secret-0000000000000001"


# --- #48: the edge's CORS matchers read a *derived* allowlist ----------------


def test_selfhost_derives_the_edge_allowlist_from_the_documented_list() -> None:
    """`EMBED_ORIGINS` is documented comma-separated; the edge needs a regex (#48).

    The Caddyfile matches `header_regexp Origin ^(…)$`, and a comma list is not a
    regex, so a correctly-configured operator's edge never matched anything. The
    deploy script derives the pipe-joined form instead of asking for a second
    spelling of the same list — and escapes `.` so `https://a.com` cannot also
    allow `https://aXcom`.
    """
    from tests.test_caddy_embed_cors import derived_origins_regex

    assert derived_origins_regex("https://a.example.com,https://b.example.com") == (
        r"https://a[.]example[.]com|https://b[.]example[.]com"
    )
    assert derived_origins_regex("https://a.example.com, https://b.example.com") == (
        r"https://a[.]example[.]com|https://b[.]example[.]com"
    ), "whitespace around an entry must not survive into the pattern"
    assert derived_origins_regex("") == ""
    assert derived_origins_regex(", ,") == "", "empty entries are dropped, like Settings does"
    assert derived_origins_regex("http://localhost:3000") == "http://localhost:3000"


def test_selfhost_recomputes_the_edge_allowlist_when_the_list_changes(tmp_path: Path) -> None:
    """Editing `EMBED_ORIGINS` and re-running must not leave a stale pattern behind.

    A stale `EMBED_ORIGINS_REGEX` is the same class of defect as a missing one: the
    edge and the API would disagree about who may embed.
    """
    env_file = tmp_path / "prod.env"
    env_file.write_text(
        "DOMAIN=localhost\n"
        f"SECRET_KEY={'s' * 48}\n"
        f"POSTGRES_PASSWORD={'p' * 24}\n"
        "AGENTCMS_IMAGE_TAG=agentcms:test\n"
        "EMBED_ORIGINS=https://old.example.com\n"
    )
    assert _run(["bash", "scripts/selfhost.sh", "--setup-only", "--env-file", str(env_file)]).returncode == 0
    assert "EMBED_ORIGINS_REGEX=https://old[.]example[.]com" in env_file.read_text()

    env_file.write_text(env_file.read_text().replace("EMBED_ORIGINS=https://old.example.com", ""))
    assert _run(["bash", "scripts/selfhost.sh", "--setup-only", "--env-file", str(env_file)]).returncode == 0
    derived = [ln for ln in env_file.read_text().splitlines() if ln.startswith("EMBED_ORIGINS_REGEX=")]
    assert derived == ["EMBED_ORIGINS_REGEX="], "an emptied allowlist must empty the pattern too"


def test_the_cors_probe_in_the_smoke_script_asks_a_path_the_api_answers() -> None:
    """#48: the smoke assertion must fail on the edge, not on a path the API 405s.

    The edge used to answer this preflight itself for *any* origin. The probe has
    to hit a path the API routes (`/embed/v1/…`), so the assertion reads the
    answer the operator actually configured.
    """
    smoke = (REPO_ROOT / "scripts" / "deploy_smoke.sh").read_text()
    assert '-X OPTIONS "${BASE_URL}/embed/v1/posts"' in smoke
    assert "Access-Control-Request-Method: GET" in smoke
    caddyfile = (REPO_ROOT / "deploy" / "compose" / "Caddyfile").read_text()
    assert "path /embed/*" in caddyfile, "the edge only short-circuits the embed surface"


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
    env["SMOKE_ADMIN_TOKEN"] = ADMIN_BOOTSTRAP_SECRET
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


def _run_smoke_against_stub(
    tmp_path: Path, *args: str, reflect: str = "", env_extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run deploy_smoke.sh against the curl stub, optionally with a reflecting edge."""
    stub_bin = tmp_path / "bin"
    if not stub_bin.exists():
        stub_bin.mkdir()
        curl = stub_bin / "curl"
        curl.write_text(CURL_STUB)
        curl.chmod(0o755)
    env = {**os.environ, "PATH": f"{stub_bin}{os.pathsep}{os.environ['PATH']}"}
    env["SMOKE_ADMIN_TOKEN"] = ADMIN_BOOTSTRAP_SECRET
    if reflect:
        env["STUB_REFLECT_ORIGIN"] = reflect
    if env_extra:
        env.update(env_extra)
    return _run(
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
            "cap_blog_ab-cd_ef",
            "--embed-token",
            "cap_blog_ro-xy",
            *args,
        ],
        env=env,
    )


def test_deploy_smoke_accepts_a_reflected_origin_the_operator_allowlisted(tmp_path: Path) -> None:
    """The positive branch: a configured allowlist must be allowed to embed (#48).

    Only the deny-all branch had ever been exercised, so nothing proved the
    assertion could still pass on a correctly-configured stack.
    """
    proc = _run_smoke_against_stub(
        tmp_path,
        "--embed-origins",
        "http://localhost:3000",
        reflect="http://localhost:3000",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "CORS preflight allowed the configured origin: http://localhost:3000" in proc.stdout, proc.stdout
    assert "ALL SMOKE TESTS PASSED" in proc.stdout, proc.stdout


def test_deploy_smoke_catches_an_edge_that_reflects_an_unlisted_origin(tmp_path: Path) -> None:
    """#48: the defect the ticket found, asserted at the level it was found.

    A stack whose edge answers `Access-Control-Allow-Origin` for an origin the
    operator never allowlisted is an open-embed hole, and the repo's own smoke
    script has to be the thing that says so.
    """
    proc = _run_smoke_against_stub(tmp_path, reflect="http://localhost:3000")
    assert proc.returncode != 0, proc.stdout
    assert "which is not in EMBED_ORIGINS" in proc.stdout + proc.stderr, proc.stdout + proc.stderr
    assert "ALL SMOKE TESTS PASSED" not in proc.stdout

    # Same verdict when an allowlist exists but does not list the probe origin.
    other = _run_smoke_against_stub(
        tmp_path,
        "--embed-origins",
        "https://a.example.com",
        reflect="http://localhost:3000",
    )
    assert other.returncode != 0, other.stdout
    assert "CORS denied" not in other.stdout, other.stdout


def test_the_e2e_checks_embed_cors_through_caddy_in_both_directions() -> None:
    """#48: the assertion has to run where the defect was, or it guards nothing.

    The E2E drives the API's published port, so every CORS assertion it already
    made sat *behind* the edge. Asserting through `${EDGE_BASE}` (Caddy) in both
    directions -- deny as deployed, allow once `EMBED_ORIGINS` is set -- is the
    only coverage that can fail if the Caddyfile matcher regresses.
    """
    e2e = (REPO_ROOT / "scripts" / "selfhost_e2e.sh").read_text()
    assert "${EDGE_BASE}/embed/v1/posts" in e2e, "the probe must hit the path the API routes"
    assert 'EDGE_BASE="${SELFHOST_E2E_EDGE_URL:-https://localhost}"' in e2e
    assert "-sk" in e2e, "Caddy serves localhost with its internal CA in the E2E"
    assert 'edge_cors_probe "$UNLISTED_PROBE_ORIGIN" deny' in e2e
    assert 'edge_cors_probe "$EMBED_PROBE_ORIGIN" allow' in e2e
    assert "./scripts/selfhost.sh --setup-only --env-file" in e2e, (
        "the allowlist must be re-derived by the same command an operator runs"
    )
    assert "^EMBED_ORIGINS_REGEX=http://localhost:3000$" in e2e, (
        "assert the derived pattern, not just the smoke result"
    )
    # A `deny` assertion that cannot fail is not an assertion.
    assert "an open-embed hole (#48)" in e2e
    assert "${acao:-no access-control-allow-origin}" in e2e


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


def test_drill_reaps_the_servers_a_killed_run_left_behind(tmp_path: Path) -> None:
    """A killed drill must not exhaust the machine's shared memory (#39, #38).

    Measured on 2026-09-25: one PostgreSQL server takes one of macOS's
    `kern.sysv.shmmni = 32` SysV segments, so 28 servers leaked by earlier killed
    runs (a harness cap, a watchdog, Ctrl-C -- the finalizer never ran) wedged the
    workstation: every later `initdb` died with `shmget(...) failed: No space left
    on device`, which is what kept the ticket pipeline's own gate red for hours and
    delayed the release that gate is meant to land.
    """
    from tests.test_backup_restore_drill import _local_server_options, _reap_orphan_servers

    options = _local_server_options(55999, Path("/tmp/agentcms-drill-pgsock-x"))
    assert "-p 55999" in options and "-k /tmp/agentcms-drill-pgsock-x" in options
    source = (REPO_ROOT / "tests" / "test_backup_restore_drill.py").read_text()
    assert "orphans = _reap_orphan_servers()" in source, (
        "a throwaway server must reap the last killed run's orphans before it starts"
    )

    root = tmp_path / "tmp"
    stale = root / "agentcms-drill-pgdata-stale"
    stale.mkdir(parents=True)
    (stale / "postmaster.pid").write_text(
        f"999999\n{stale}\n1\n55999\n{root / 'sock-stale'}\n127.0.0.1\n\nready\n"
    )
    two_hours_ago = time.time() - 7200
    os.utime(stale / "postmaster.pid", (two_hours_ago, two_hours_ago))
    fresh = root / "agentcms-drill-pgdata-fresh"
    fresh.mkdir()
    (fresh / "postmaster.pid").write_text("999998\nx\n1\n2\n3\n4\n5\nready\n")

    assert _reap_orphan_servers(root=root) == ["agentcms-drill-pgdata-stale"]
    assert not stale.exists(), "a stale drill server must be stopped and deleted"
    assert fresh.exists(), "a concurrent test's server must never be touched"


def test_selfhost_e2e_reads_base64url_capability_tokens_whole() -> None:
    """#37/#44: capability tokens are base64url, so ``-`` and ``_`` are legal characters.

    ``scripts/selfhost_e2e.sh`` used to read the seeded token with ``cap_[A-Za-z0-9_]+``;
    the random half of ``cap_blog_tKy9A-b_CdEf...`` then matched as ``cap_blog_tKy``.  The
    API answered ``401 Unknown capability token`` and the deploy job blamed the product
    (1-in-N runs passed, so it read as flakiness).  This runs the script's **own** patterns
    over the exact lines ``scripts/seed.py`` prints, so narrowing them again fails here
    instead of in the self-host deploy job.
    """
    script = (REPO_ROOT / "scripts" / "selfhost_e2e.sh").read_text()
    seed_src = (REPO_ROOT / "scripts" / "seed.py").read_text()

    write_match = re.search(r"grep -oE '(cap_[^']+)'", script)
    assert write_match, "the script must read the write token with `grep -oE 'cap_...'`"

    # The read-only token is captured by a `sed` group, so read the group out of the script.
    sed_line = next((line for line in script.splitlines() if "Embed token (read-only)" in line), "")
    assert "sed -n" in sed_line, "the script must read the read-only embed token with a `sed` capture"
    captured = sed_line.split("\\(", 1)[1].split("\\)", 1)[0]
    assert captured.startswith("cap_"), captured

    assert r"tr -d '\r'" in script, "`compose exec` output is CRLF: the token must lose the CR"

    # The labels are the only thing the sed capture can key on -- and they come from seed.py.
    assert '"  Capability token:  {token}"' in seed_src
    assert '"  Embed token (read-only): {embed_token}"' in seed_src

    write_token = "cap_blog_tKy9A-b_CdEf-GhIjKlMnOpQr"
    embed_token = "cap_blog_9x_Yz-12_AbCdEfGhIjKlMn"
    seed_stdout = (
        "Seed complete for AgentCMS.\r\n"
        f"  Capability token:  {write_token}\r\n"
        f"  Embed token (read-only): {embed_token}\r\n"
        "  Instruction sheet: GET  /c/" + write_token + "\r\n"
    )

    def extract(command: str, pattern: str) -> str:
        """Run one extraction pipeline from the script, under the script's own shell flags."""
        completed = subprocess.run(
            ["bash", "-c", f"set -euo pipefail; {command}", "--", pattern],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "SEED_OUT": seed_stdout},
        )
        assert completed.returncode == 0, completed.stderr
        return completed.stdout.strip()

    written = extract(
        r"""printf '%s\n' "$SEED_OUT" | tr -d '\r' | grep -oE "$1" | head -1""",
        write_match.group(1),
    )
    assert written == write_token, f"the write token was truncated: got {written!r}"

    embedded = extract(
        r"""printf '%s\n' "$SEED_OUT" | tr -d '\r'"""
        r""" | sed -n "s|.*Embed token (read-only): *\($1\).*|\1|p" | head -1""",
        captured,
    )
    assert embedded == embed_token, f"the read-only token was truncated: got {embedded!r}"
    assert embedded != written, "the embed surface must never get the write token"


def test_the_capability_token_alphabet_is_base64url() -> None:
    """The fixture above is only meaningful if the service really mints ``-`` and ``_``.

    Asserting it here means the guard cannot quietly become a test of a token shape
    nothing produces (``cap_<site_slug>_<secrets.token_urlsafe>``, #28).
    """
    from app.services.capability_tokens import generate_capability_token

    token, _ = generate_capability_token("blog")
    assert re.fullmatch(r"cap_blog_[A-Za-z0-9_-]+", token), token
    alphabet = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
    assert set(token) <= alphabet | {"c", "a", "p", "_", "b", "l", "o", "g"}


# --- #44: the admin surface is closed, so the smoke scripts must authenticate ---


def test_deploy_smoke_authenticates_the_admin_mint() -> None:
    """#44: POST /v1/admin/tokens needs the bootstrap secret -- no anonymous fallback.

    A curl from the host to a published port is not a loopback peer, so a smoke
    script that probed the mint unauthenticated would report the guard as a broken
    product.  It must send ``X-Admin-Token`` and must stop before probing when the
    secret is missing.
    """
    body = (REPO_ROOT / "scripts" / "deploy_smoke.sh").read_text()
    block = body.split("# ---- 3. Create admin token ----", 1)[1][:1500]

    assert "X-Admin-Token: ${ADMIN_BOOTSTRAP_TOKEN}" in block, block
    assert "No admin bootstrap secret" in block, block
    assert "exit 1" in block, "a missing secret must be fatal, not a warning"
    assert "--admin-token" in body


def test_upgrade_smoke_authenticates_the_admin_mint() -> None:
    """The upgrade path mints a token too, so it needs the same secret (#44)."""
    body = (REPO_ROOT / "scripts" / "upgrade_smoke.sh").read_text()
    mint = body.split("create_admin_token()", 1)[1].split("\n}", 1)[0]

    assert "X-Admin-Token: ${SMOKE_ADMIN_TOKEN}" in mint, mint
    assert "log_error" in mint and "return 1" in mint, mint
    assert "/v1/admin/tokens" in mint


def test_selfhost_e2e_hands_the_bootstrap_secret_to_the_smoke_script() -> None:
    """The E2E must read ADMIN_TOKEN out of the generated .env (it cannot be baked in)."""
    body = (REPO_ROOT / "scripts" / "selfhost_e2e.sh").read_text()

    assert "export SMOKE_ADMIN_TOKEN=" in body
    assert "^ADMIN_TOKEN=" in body, "the bootstrap secret must come from the generated .env"
    marker = body.index("export SMOKE_ADMIN_TOKEN=")
    assert "die " in body[max(0, marker - 400) : marker], (
        "a missing ADMIN_TOKEN must abort the E2E rather than skip the admin assertions"
    )


def test_selfhost_setup_generates_an_admin_token() -> None:
    """A self-hoster must never have to invent the bootstrap secret by hand."""
    body = (REPO_ROOT / "scripts" / "selfhost.sh").read_text()
    assert "ADMIN_TOKEN" in body
    assert "openssl rand" in body or "secrets" in body, "ADMIN_TOKEN must be generated"


def test_deploy_docs_pin_a_released_image_digest() -> None:
    """#38: the deploy docs must pin a real released digest, not a tag or a placeholder."""
    import tomllib

    version = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["project"]["version"]
    tag = f"v{version}"
    deploying = (REPO_ROOT / "docs/DEPLOYING.md").read_text()
    quickstart = (REPO_ROOT / "docs/deploy/quickstart.md").read_text()
    release = (REPO_ROOT / ".github/workflows/release.yml").read_text()
    digest = re.compile(r"ghcr\.io/smartalfred/agentcms@sha256:[0-9a-f]{64}")

    assert digest.search(deploying), "docs/DEPLOYING.md must pin the released image by digest"
    assert digest.search(quickstart), "docs/deploy/quickstart.md must show the digest to pin"
    assert tag in deploying, f"docs/DEPLOYING.md must name the current release tag {tag}"
    # The release workflow used to skip recording the digest whenever the notes merely
    # mentioned 'sha256:', so a release could ship with no pin. Key off a marker instead.
    assert "agentcms-image-digest" in release
    assert "grep -q 'sha256:'" not in release


# --- script -> API drift: a script may not call a route the app does not serve ---

# A route registered with ``include_in_schema=False`` is real but invisible to
# ``app.openapi()``, so it cannot be discovered from the schema; list it here and
# assert below that the app source still declares it.
_SCHEMA_INVISIBLE_SCRIPT_ROUTES = ("/v1/version",)

# A /v1 call in a shell script, in either shape it can take: built from a variable
# (``"${BASE_URL}/v1/sites/${SITE_SLUG}/posts"``) or spelled out absolutely
# (``"http://127.0.0.1:8000/v1/admin/tokens"``).  The second pattern exists so that
# dropping the ``${BASE_URL}`` indirection cannot hide a call from this guard.
_SCRIPT_V1_URLS = (
    re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}(/v1/[A-Za-z0-9_./${}-]*)"),
    re.compile(r'(?:^|["\'\s=])(?:https?://[A-Za-z0-9_.:-]+)?(/v1/[A-Za-z0-9_./${}-]*)'),
)
_PATH_PARAM = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}|\{[A-Za-z_][A-Za-z0-9_]*\}")


def _template(path: str) -> str:
    """``/v1/sites/${SITE_SLUG}/posts`` -> ``/v1/sites/{}/posts``; params go opaque.

    Any path segment the API declares as a parameter must be a parameter in the
    script too: ``/v1/sites/blog/posts`` is *not* the route the app serves, and a
    guard that allowed a literal there would approve a hardcoded tenant.
    """
    return _PATH_PARAM.sub("{}", path.rstrip("/") or "/")


def _script_v1_calls() -> list[tuple[str, int, str | None, str]]:
    """(script, line, method, path-template) for every ``scripts/*.sh`` /v1 call.

    Discovery reads the curl command, *not* ``app.routes``: under FastAPI 0.141
    ``include_router`` appends opaque ``_IncludedRouter`` wrappers, so ``app.routes``
    exposes zero ``/v1/admin`` paths and a sweep built on it passes vacuously -- the
    trap #44's own guard test fell into.  Continuations are joined first, because a
    call and its URL are usually on different lines.
    """
    calls: set[tuple[str, int, str | None, str]] = set()
    for script in sorted((REPO_ROOT / "scripts").glob("*.sh")):
        lines: list[tuple[int, str]] = []
        for number, line in enumerate(script.read_text().splitlines(), 1):
            if lines and lines[-1][1].endswith("\\"):
                lines[-1] = (lines[-1][0], lines[-1][1][:-1] + " " + line.strip())
            else:
                lines.append((number, line))
        for number, line in lines:
            if line.lstrip().startswith("#") or "curl" not in line:
                continue
            method = re.search(r"(?:^|\s)-X\s*([A-Za-z]+)", line)
            for pattern in _SCRIPT_V1_URLS:
                for match in pattern.finditer(line):
                    calls.add(
                        (
                            script.name,
                            number,
                            method.group(1).upper() if method else None,
                            _template(match.group(1).split("?", 1)[0]),
                        )
                    )
    return sorted(calls)


def test_scripts_only_call_v1_routes_the_app_serves() -> None:
    """A script that calls a route the API does not serve must fail here, not in CI.

    Nothing checked this: the quickstart documented a ``capability-links`` route
    that does not exist, and the only place such a call surfaces is a deploy smoke
    run in CI (or an operator's deploy).  The route set comes from
    ``app.openapi()["paths"]`` -- the artefact the product actually serves -- plus
    the schema-invisible routes listed above.
    """
    from app.main import app

    spec = app.openapi()["paths"]
    served = {_template(path) for path in spec}
    served |= {_template(path) for path in _SCHEMA_INVISIBLE_SCRIPT_ROUTES}
    allowed_methods = {_template(path): {op.lower() for op in ops} for path, ops in spec.items()}

    calls = _script_v1_calls()
    called = {call[3] for call in calls}
    # A guard whose discovery silently finds nothing approves everything: assert the
    # two load-bearing calls were found before believing any of the above.
    assert "/v1/admin/tokens" in called, f"discovery found no admin call: {sorted(called)}"
    assert "/v1/sites/{}/posts" in called, f"discovery found no site call: {sorted(called)}"
    assert len(calls) >= 6, f"discovery found only {len(calls)} calls: {sorted(called)}"

    problems = []
    for script, line, method, path in calls:
        if path not in served:
            problems.append(f"{script}:{line}: calls {path}, which the API does not serve")
        elif method and path in allowed_methods and method.lower() not in allowed_methods[path]:
            problems.append(
                f"{script}:{line}: {method} {path} is not allowed ({sorted(allowed_methods[path])})"
            )
    assert not problems, "scripts calling routes that do not exist: " + "; ".join(problems)


def test_the_schema_invisible_script_routes_are_really_declared() -> None:
    """The allowlist above must not become a bag of names nobody serves."""
    source = "\n".join(path.read_text() for path in (REPO_ROOT / "app").rglob("*.py"))
    for path in _SCHEMA_INVISIBLE_SCRIPT_ROUTES:
        assert f'"{path}"' in source, f"{path} is allowlisted for scripts but no longer declared"


def test_the_smoke_fails_when_the_admin_surface_answers_an_anonymous_caller(
    tmp_path: Path,
) -> None:
    """#44: the deploy smoke must go red if /v1/admin/* serves an anonymous caller.

    Watched failing before it was believed: with ``STUB_ADMIN_ANON_CODE=201`` the
    stub plays the stack #44 shipped (or a loopback-exempt vantage) and the smoke
    exits 1 with ``#44 REGRESSION``.  ``STUB_ADMIN_MINT_CODE=401`` covers the
    opposite lie -- a stack that refuses *everything*, including the correct
    secret, must fail the positive control instead of passing the refusal.
    """
    allowed = _run_smoke_against_stub(tmp_path, env_extra={"STUB_ADMIN_ANON_CODE": "201"})
    assert allowed.returncode != 0, allowed.stdout + allowed.stderr
    assert "#44 REGRESSION" in allowed.stdout + allowed.stderr, allowed.stdout
    assert "loopback" in allowed.stdout + allowed.stderr, "the exemption must be named"

    refuses_all = _run_smoke_against_stub(tmp_path, env_extra={"STUB_ADMIN_MINT_CODE": "401"})
    assert refuses_all.returncode != 0, refuses_all.stdout + refuses_all.stderr
    assert "Positive control failed" in refuses_all.stdout, refuses_all.stdout


def test_selfhost_e2e_asserts_the_anonymous_refusal_itself() -> None:
    """The job that boots the published image must carry its own #44 assertion.

    deploy_smoke.sh asserts the refusal too, but release.yml boots the published
    digest through selfhost_e2e.sh: if the probe lived only in the smoke script, a
    missing guard could still be reported by a job whose log never mentions the
    admin surface (the deployed job log has 0 occurrences of ``v1/admin`` today).
    """
    body = (REPO_ROOT / "scripts" / "selfhost_e2e.sh").read_text()
    block = body.split("asserting /v1/admin/* refuses an anonymous caller", 1)[1]
    block = block.split("# --- 7.", 1)[0]

    assert "#44 REGRESSION" in block, block
    assert block.count('"${BASE_URL}/v1/admin/tokens"') >= 2, "POST and GET must both be probed"
    assert '-X POST "${BASE_URL}/v1/admin/tokens"' in block, "the mint probe must be a POST"
    assert "X-Admin-Token: ${bootstrap_admin_token}" in block, "the positive control must send the secret"
    assert "'201'" in block or '"201"' in block, "the positive control must require 201"
    assert "loopback" in block, "a loopback-exempt vantage must be named, not silently passed"
