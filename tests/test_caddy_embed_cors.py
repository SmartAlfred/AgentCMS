"""The edge must not assert a CORS permission the operator denied (#48).

``deploy/compose/Caddyfile`` used to answer *any* ``OPTIONS`` request carrying an
``Access-Control-Request-Method`` with ``204`` and
``Access-Control-Allow-Origin: {the caller's Origin}`` plus
``Access-Control-Allow-Credentials: true`` — answered at the edge, before the API
ran, with no reference to ``EMBED_ORIGINS``.  On a stock deploy
(``EMBED_ORIGINS=``, the documented deny-all default) that made the edge advertise
open embedding, and made the repository's own ``scripts/deploy_smoke.sh`` CORS
assertion unsatisfiable.

Nothing tested the Caddyfile's matchers, so they are tested here.  Caddy is not
needed: the file is parsed, its ``{$ENV:default}`` placeholders expanded the way
Caddy expands them, and its matchers evaluated — so these assertions are about the
behaviour of the shipped configuration, not about the text of an assertion.  A
``caddy validate`` test at the bottom covers real adaptation where Docker is around.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from functools import cache, lru_cache
from pathlib import Path

import pytest

from tests.pg import skip_or_fail_without_docker

REPO_ROOT = Path(__file__).resolve().parents[1]
CADDYFILE = REPO_ROOT / "deploy" / "compose" / "Caddyfile"

# Origins that are on nobody's allowlist, including near misses of the ones the
# tests *do* allow: an unescaped `.` in the derived pattern would match all of these.
UNLISTED_ORIGINS = (
    "https://evil.example",
    "https://aXexample.com",
    "https://wwwXexampleYcom",
    "https://sub.a.example.com",
    "http://localhost:3001",
    "null",
)

CORS_HEADER_NAMES = (
    "Access-Control-Allow-Origin",
    "Access-Control-Allow-Methods",
    "Access-Control-Allow-Headers",
    "Access-Control-Allow-Credentials",
    "Access-Control-Max-Age",
    "Vary",
)


# ---------------------------------------------------------------------------
# A small Caddyfile reader
# ---------------------------------------------------------------------------


@dataclass
class Block:
    """One Caddyfile block: ``head`` is its head line, plus body lines/children."""

    head: str
    lines: list[str] = field(default_factory=list)
    children: list[Block] = field(default_factory=list)


def _tokenize(line: str) -> list[str]:
    """Split a Caddyfile line into tokens, honouring double quotes.

    Inline comments are not used by this file and are rejected rather than silently
    dropped: a tokeniser that quietly ate a ``#`` inside a regex would make every
    assertion below vacuous.
    """
    tokens: list[str] = []
    current = ""
    quoted = False
    for char in line:
        if char == '"':
            quoted = not quoted
            continue
        if char == "#" and not quoted:
            raise AssertionError(f"inline comment not modelled by this test: {line!r}")
        if char.isspace() and not quoted:
            if current:
                tokens.append(current)
                current = ""
            continue
        current += char
    if quoted:
        raise AssertionError(f"unterminated quote: {line!r}")
    if current:
        tokens.append(current)
    return tokens


def _parse(lines: list[str]) -> list[Block]:
    """Parse Caddyfile source into a block tree, ignoring comments and blanks."""
    root = Block("<root>")
    stack = [root]
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line == "}":
            if len(stack) == 1:
                raise AssertionError(f"unbalanced closing brace in {CADDYFILE}")
            stack.pop()
            continue
        tokens = _tokenize(line)
        if not tokens:
            continue
        if tokens[-1] == "{":
            node = Block(" ".join(tokens[:-1]))
            stack[-1].children.append(node)
            stack.append(node)
            continue
        stack[-1].lines.append(line)
    if len(stack) != 1:
        raise AssertionError(f"unterminated block in {CADDYFILE}")
    return root.children


@lru_cache(maxsize=1)
def _site() -> Block:
    """Return the single site block — the file must not define the site twice (#37)."""
    top = _parse(CADDYFILE.read_text().splitlines())
    global_options = [block for block in top if "admin" in block.head]
    sites = [block for block in top if "DOMAIN" in block.head]
    assert not global_options, "the admin API is configured inside the global options block"
    assert len(sites) == 1, f"expected exactly one site block, found {[b.head for b in top]}"
    return sites[0]


def _expand_env(token: str, env: Mapping[str, str]) -> str:
    """Expand ``{$NAME}`` / ``{$NAME:default}`` the way Caddy's replaceEnvVars does.

    An unset variable with no default expands to the empty string, which is what
    lets the Caddyfile adapt in every mode (#37).
    """

    def replace(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        return env.get(name, "" if default is None else default)

    return ENV_PLACEHOLDER.sub(replace, token)


# Caddy's own env-var regexp, from caddyconfig/caddyfile/parse.go.
ENV_PLACEHOLDER = re.compile(r"\{\$([a-zA-Z0-9_]+)(?::([^}]*))?\}")


@dataclass
class Request:
    method: str
    path: str
    headers: Mapping[str, str] = field(default_factory=dict)
    host: str = "localhost"


# ---------------------------------------------------------------------------
# Matcher evaluation
# ---------------------------------------------------------------------------


def _match_glob(pattern: str, value: str) -> bool:
    """Caddy's ``*`` wildcard for ``path``/``header``: zero or more of anything."""
    return re.fullmatch(re.escape(pattern).replace(r"\*", ".*"), value, re.DOTALL) is not None


def _header_values(headers: Mapping[str, str], name: str) -> list[str]:
    return [value for key, value in headers.items() if key.lower() == name.lower()]


def _matches_header(spec: tuple[str, str], headers: Mapping[str, str], *, regex: bool) -> bool:
    name, pattern = spec
    for value in _header_values(headers, name):
        if regex and re.search(pattern, value):
            return True
        if not regex and _match_glob(pattern, value):
            return True
    return False


def _matcher_matches(matcher: list[str], env: Mapping[str, str], request: Request) -> bool:
    """Evaluate one matcher line against a request (Caddy ANDs the conditions)."""
    kind, rest = matcher[0], matcher[1:]
    if kind == "method":
        return any(request.method == candidate.upper() for candidate in rest)
    if kind == "path":
        return any(_match_glob(pattern, request.path) for pattern in rest)
    if kind == "host":
        return request.host in rest
    if kind == "header":
        # `header <name> *` means "this header is present at all".
        pattern = " ".join(rest[1:]) or "*"
        if pattern == "*":
            return bool(_header_values(request.headers, rest[0]))
        return _matches_header((rest[0], pattern), request.headers, regex=False)
    if kind == "header_regexp":
        return _matches_header((rest[0], " ".join(rest[1:])), request.headers, regex=True)
    if kind == "not":
        return not _matcher_matches(rest, env, request)
    raise AssertionError(f"matcher not modelled by this test: {' '.join(matcher)!r}")


# ---------------------------------------------------------------------------
# What the edge would answer
# ---------------------------------------------------------------------------


def edge_cors_headers(env: Mapping[str, str], request: Request) -> dict[str, str]:
    """Return the CORS headers the edge emits for this request, or ``{}`` when quiet.

    ``handle`` sorts ahead of ``reverse_proxy`` in Caddy's directive order, so the
    first ``handle`` block whose matcher matches wins and nothing is proxied; the
    blocks here therefore are the edge's whole answer for a request.  A header
    whose value expands to the empty string is *not* emitted — Caddy removes a
    header with an empty value, which is why even the deny-all ``^()$`` pattern
    cannot produce an empty ``Access-Control-Allow-Origin``.
    """
    site = _site()
    matchers: dict[str, Block] = {}
    for block in site.children:
        if block.head.startswith("@"):
            assert block.head[1:] not in matchers, f"{block.head} is declared twice"
            matchers[block.head[1:]] = block
    for block in site.children:
        if not block.head.startswith("handle "):
            continue
        name = block.head.removeprefix("handle ").removeprefix("@")
        gate = matchers.get(name)
        assert gate is not None, f"`{name}` is used as a matcher but never declared"
        conditions = [[_expand_env(token, env) for token in _tokenize(line)] for line in gate.lines]
        if not all(_matcher_matches(cond, env, request) for cond in conditions):
            continue
        headers: dict[str, str] = {}
        for line in block.lines:
            tokens = [token for token in _tokenize(line) if token]
            if len(tokens) < 3 or tokens[0] != "header" or tokens[1] not in CORS_HEADER_NAMES:
                continue
            value = " ".join(tokens[2:]).replace("{header.Origin}", request.headers.get("Origin", ""))
            value = _expand_env(value, env)
            if value:
                headers[tokens[1]] = value
        return headers
    return {}


# ---------------------------------------------------------------------------
# The derived allowlist, produced by the shipped deploy script
# ---------------------------------------------------------------------------


@cache
def derived_origins_regex(origins: str) -> str:
    """Run ``scripts/selfhost.sh`` and return the ``EMBED_ORIGINS_REGEX`` it wrote.

    Deliberately not re-implemented here: the Caddyfile consumes whatever the
    deploy script derives, so that is what the assertions below evaluate.
    """
    with tempfile.TemporaryDirectory() as tmp:
        env_file = Path(tmp) / "prod.env"
        env_file.write_text(
            "DOMAIN=localhost\n"
            f"SECRET_KEY={'s' * 48}\n"
            f"POSTGRES_PASSWORD={'p' * 24}\n"
            "AGENTCMS_IMAGE_TAG=agentcms:test\n"
            f"EMBED_ORIGINS={origins}\n"
        )
        proc = subprocess.run(
            [
                "bash",
                str(REPO_ROOT / "scripts" / "selfhost.sh"),
                "--setup-only",
                "--env-file",
                str(env_file),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
            env={key: value for key, value in os.environ.items() if key != "EMBED_ORIGINS"},
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        generated = env_file.read_text()
        derived = [
            line.partition("=")[2] for line in generated.splitlines() if "EMBED_ORIGINS_REGEX=" in line
        ]
        # A deny-all deployment derives nothing, and the key is then absent.
        assert len(derived) <= 1, generated
        return derived[0] if derived else ""


def _env(origins: str) -> dict[str, str]:
    return {"DOMAIN": "localhost", "EMBED_ORIGINS_REGEX": derived_origins_regex(origins)}


def _preflight(origin: str, path: str = "/embed/v1/posts") -> Request:
    return Request(
        method="OPTIONS",
        path=path,
        headers={"Origin": origin, "Access-Control-Request-Method": "GET"},
    )


# ---------------------------------------------------------------------------
# The regression
# ---------------------------------------------------------------------------


def test_the_preflight_matcher_is_constrained_by_the_allowlist() -> None:
    """The ticket's repro, verbatim: deny-all must not reflect any Origin."""
    assert derived_origins_regex("") == "", "an empty allowlist must derive an empty pattern"
    env = _env("")  # the documented default in deploy/.env.example

    for origin in ("https://evil.example", *UNLISTED_ORIGINS):
        assert edge_cors_headers(env, _preflight(origin)) == {}, origin


def test_an_unlisted_origin_is_denied_even_when_one_is_listed() -> None:
    """``EMBED_ORIGINS=http://localhost:3000`` must not open up to anyone else."""
    env = _env("http://localhost:3000")

    allowed = edge_cors_headers(env, _preflight("http://localhost:3000"))
    assert allowed["Access-Control-Allow-Origin"] == "http://localhost:3000"
    assert allowed["Access-Control-Allow-Credentials"] == "true"

    for origin in UNLISTED_ORIGINS:
        assert edge_cors_headers(env, _preflight(origin)) == {}, origin


def test_a_preflight_without_an_origin_header_advertises_nothing() -> None:
    """The deny-all pattern ``^()$`` does match an *absent* Origin.

    Caddy drops a header whose value is empty, so the edge must not turn that into
    an ``Access-Control-Allow-Origin:`` line either.
    """
    request = Request(method="OPTIONS", path="/embed/v1/posts", headers={})
    assert edge_cors_headers(_env(""), request) == {}
    assert edge_cors_headers(_env("http://localhost:3000"), request) == {}


def test_every_allowlisted_origin_is_allowed_and_nothing_else_is() -> None:
    """The derived pattern must match the configured list exactly, not a superset."""
    env = _env("https://a.example.com, https://b.example.com")

    for origin in ("https://a.example.com", "https://b.example.com"):
        assert edge_cors_headers(env, _preflight(origin))["Access-Control-Allow-Origin"] == origin

    for origin in UNLISTED_ORIGINS:
        assert edge_cors_headers(env, _preflight(origin)) == {}, origin


def test_the_edge_does_not_widen_cors_beyond_the_embed_surface() -> None:
    """An allowlisted origin must not collect ACAO on admin or capability paths.

    ``handle @embed_origin`` matched *any* path, so an embedding origin was handed
    ``Access-Control-Allow-Credentials: true`` on the admin JSON and the dashboard
    as well as on the embed feed.
    """
    env = _env("https://a.example.com")
    for path in ("/v1/admin/posts", "/c/cap_blog_abcdef/posts", "/dashboard", "/blog/posts.json"):
        actual = Request("GET", path, {"Origin": "https://a.example.com"})
        assert edge_cors_headers(env, actual) == {}, path
        assert edge_cors_headers(env, _preflight("https://a.example.com", path)) == {}, path


def test_the_preflight_short_circuit_only_covers_options() -> None:
    """A real GET is proxied, so the edge adds no preflight-only headers to it."""
    env = _env("https://a.example.com")
    actual = Request("GET", "/embed/v1/posts", {"Origin": "https://a.example.com"})
    assert edge_cors_headers(env, actual) == {
        "Access-Control-Allow-Origin": "https://a.example.com",
        "Access-Control-Allow-Credentials": "true",
        "Vary": "Origin",
    }
    assert "Access-Control-Allow-Methods" not in edge_cors_headers(env, actual)

    preflight = edge_cors_headers(env, _preflight("https://a.example.com"))
    assert preflight["Access-Control-Allow-Methods"] == "GET, POST, OPTIONS"
    assert preflight["Access-Control-Max-Age"] == "86400"
    assert preflight["Vary"] == "Origin", "a 24h-cached preflight must vary by Origin"


def test_every_cors_header_the_edge_can_emit_is_behind_both_gates() -> None:
    """Structural backstop: no CORS header may be written outside a gated block."""
    source = [line for line in CADDYFILE.read_text().splitlines() if not line.strip().startswith("#")]
    for name in ("Access-Control-Allow-Origin", "Access-Control-Allow-Credentials"):
        assert sum(name in line for line in source) == 2, (
            f"the edge writes `{name}` in {sum(name in line for line in source)} places"
        )

    site = _site()
    declarations = {block.head[1:]: block for block in site.children if block.head.startswith("@")}
    emitting = 0
    for block in site.children:
        if block.head.startswith("@"):
            continue  # a matcher declares a condition; it never writes a response header
        cors_lines = [line for line in block.lines if "Access-Control" in line]
        if not cors_lines:
            continue
        emitting += 1
        assert block.head.startswith("handle @embed_"), f"CORS written outside a gate: {block.head}"
        gate = declarations[block.head.removeprefix("handle ").removeprefix("@")]
        conditions = " ".join(gate.lines)
        assert "header_regexp Origin ^({$EMBED_ORIGINS_REGEX})$" in conditions, conditions
        assert "path /embed/*" in conditions, conditions
    assert emitting == 2, "both CORS-emitting blocks must be gated"


def test_the_edge_never_answers_a_preflight_for_a_non_embed_path() -> None:
    """The capability-link surface gets no edge CORS, whatever the allowlist says."""
    env = _env("https://a.example.com")
    assert edge_cors_headers(env, _preflight("https://a.example.com", "/c/cap_x/posts")) == {}


# ---------------------------------------------------------------------------
# The deploy script and the shipped compose bundle agree on the variable
# ---------------------------------------------------------------------------


def test_the_compose_bundle_passes_the_derived_allowlist_to_the_edge() -> None:
    compose = (REPO_ROOT / "deploy" / "compose" / "docker-compose.prod.yml").read_text()
    assert "EMBED_ORIGINS_REGEX: ${EMBED_ORIGINS_REGEX:-}" in compose, (
        "the caddy service must receive the derived allowlist: EMBED_ORIGINS is "
        "comma-separated and cannot be used as a regex (#48)"
    )


def test_the_derived_allowlist_is_documented_as_derived() -> None:
    template = REPO_ROOT / "deploy" / ".env.example"
    text = template.read_text()
    assert "EMBED_ORIGINS_REGEX" in text, f"{template} must document the derived allowlist"
    assert not re.search(r"^EMBED_ORIGINS_REGEX=.+$", text, re.M), (
        "the template must leave the derived value empty: scripts/selfhost.sh computes it"
    )
    config_doc = (REPO_ROOT / "docs" / "deploy" / "configuration.md").read_text()
    assert "EMBED_ORIGINS_REGEX" in config_doc, "the derived allowlist must be documented"


# ---------------------------------------------------------------------------
# Real adaptation, when Caddy can be run
# ---------------------------------------------------------------------------


def _caddy_validate(env: Mapping[str, str]) -> subprocess.CompletedProcess[str]:
    env_args = [arg for key, value in sorted(env.items()) for arg in ("-e", f"{key}={value}")]
    return subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            *env_args,
            "-v",
            f"{CADDYFILE}:/etc/caddy/Caddyfile:ro",
            "caddy:2-alpine",
            "caddy",
            "validate",
            "--config",
            "/etc/caddy/Caddyfile",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture(scope="module")
def caddy_container() -> Iterator[None]:
    """Skip unless Caddy can actually be run here (a Docker daemon and its image)."""
    if os.environ.get("AGENTCMS_SKIP_CADDY"):
        pytest.skip("AGENTCMS_SKIP_CADDY is set")
    skip_or_fail_without_docker("Caddyfile adaptation")
    probe = subprocess.run(
        ["docker", "run", "--rm", "caddy:2-alpine", "caddy", "version"],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        pytest.skip(f"the caddy image cannot be run here: {probe.stderr[-400:]}")
    yield


@pytest.mark.parametrize(
    "origins",
    ["", "http://localhost:3000", "https://a[.]example[.]com|https://b[.]example[.]com"],
    ids=["deny-all", "single-origin", "two-origins"],
)
def test_the_caddyfile_still_adapts(caddy_container: None, origins: str) -> None:
    """``caddy validate`` in each allowlist mode — #37 made adaptation a hard failure."""
    result = _caddy_validate(_env(origins) | {"DOMAIN": "localhost"})
    assert result.returncode == 0, result.stdout + result.stderr
