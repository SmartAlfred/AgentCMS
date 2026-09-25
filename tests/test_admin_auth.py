"""#44: every ``/v1/admin/*`` route is authenticated (regression tests).

Two production blockers shipped through the human dashboard precisely because
that surface had no tests.  The admin surface had the same shape: 18 routes
mounted with ``require_auth``, for which ``scope_for_endpoint()`` returns
``None`` -- so ``POST /v1/admin/tokens`` with no credentials at all minted a
scoped admin token for anyone who could reach the instance.

These tests therefore come in two flavours: behavioural (what a caller with and
without each credential gets) and structural (no admin route can ever mount
again without the guard).
"""

from __future__ import annotations

import re

import pytest
from app.api.admin_auth import ADMIN_SCOPE, require_admin
from app.config import Settings
from app.dashboard.auth import (
    CSRF_COOKIE,
    CSRF_TOKEN_HEADER,
    SESSION_COOKIE,
    _sign_csrf,
    create_session_token,
    generate_csrf_token,
)
from fastapi.testclient import TestClient

PROD_SECRET = "production-secret-key-long-enough-00000000"
PROD_ADMIN_TOKEN = "production-admin-bootstrap-token-000001"
PROD_IMAGE_TAG = "ghcr.io/smartalfred/agentcms:v0.3.1"

_UUID = "00000000-0000-0000-0000-000000000000"
_PATH_PARAM = re.compile(r"\{[^}]*\}")


def _production_env(database_url: str, monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    values = {
        "APP_ENV": "production",
        "DATABASE_URL": database_url,
        "SECRET_KEY": PROD_SECRET,
        "AGENTCMS_IMAGE_TAG": PROD_IMAGE_TAG,
        "ADMIN_TOKEN": PROD_ADMIN_TOKEN,
    }
    values.update(overrides)
    for key, value in values.items():
        monkeypatch.setenv(key, value)


@pytest.fixture()
def prod_settings(database_url: str) -> Settings:
    return Settings(
        _env_file=None,
        app_env="production",
        database_url=database_url,
        secret_key=PROD_SECRET,
        agentcms_image_tag=PROD_IMAGE_TAG,
        admin_token=PROD_ADMIN_TOKEN,
        log_level="WARNING",
    )


@pytest.fixture()
def prod_client(
    prod_settings: Settings, database_url: str, db, monkeypatch: pytest.MonkeyPatch
) -> TestClient:
    """A production-configured app on the throwaway database.

    Only production can show the guard failing closed: ``APP_ENV=test`` is
    exempt (mirroring ``GET /metrics``) so the 47 unauthenticated
    ``/v1/admin/*`` call sites elsewhere in the suite keep working.
    """
    _production_env(database_url, monkeypatch)
    from app.main import create_app

    with TestClient(create_app(prod_settings)) as client:
        yield client


def _admin_paths(client: TestClient) -> list[str]:
    paths = {route.path for route in client.app.routes if getattr(route, "path", "").startswith("/v1/admin")}
    return sorted(_PATH_PARAM.sub(_UUID, path) for path in paths)


# ---------------------------------------------------------------------------
# Behavioural: no credentials
# ---------------------------------------------------------------------------


def test_admin_tokens_post_without_credentials_is_refused(prod_client: TestClient) -> None:
    """The #44 regression: this returned 201 for anyone who could reach the app."""
    response = prod_client.post("/v1/admin/tokens", json={"label": "attacker", "scopes": ["posts:write"]})

    assert response.status_code == 401, response.text
    body = response.json()
    assert body["status"] == 401
    assert "admin" in body["detail"].lower()
    assert body["hint"]


def test_every_admin_route_refuses_anonymous_requests(prod_client: TestClient) -> None:
    """Sweep: no admin route may answer an unauthenticated caller with success."""
    paths = _admin_paths(prod_client)
    assert len(paths) >= 15, f"only found {len(paths)} admin routes: {paths}"

    leaks: dict[str, list[str]] = {}
    for path in paths:
        for method in ("GET", "POST", "DELETE"):
            response = prod_client.request(method, path, json={})
            if response.status_code in {401, 403, 405}:
                continue
            leaks.setdefault(path, []).append(f"{method} -> {response.status_code}")

    assert not leaks, f"unauthenticated callers got a non-refusal from: {leaks}"


def test_forwarded_for_header_does_not_grant_loopback_trust(prod_client: TestClient) -> None:
    """`X-Forwarded-For` is client-controlled: it must never satisfy the guard.

    ``/metrics`` honours it; the admin surface deliberately does not, or a
    remote caller could claim to be 127.0.0.1 and mint a token.
    """
    response = prod_client.post(
        "/v1/admin/tokens",
        json={"label": "spoofed", "scopes": ["posts:write"]},
        headers={"X-Forwarded-For": "127.0.0.1"},
    )

    assert response.status_code == 401, response.text


def test_loopback_peer_is_exempt(prod_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """The documented exemption (same shape as /metrics), and its boundary."""
    from app.main import create_app

    monkeypatch.setattr("app.api.admin_auth.get_settings", lambda: prod_settings)
    with TestClient(create_app(prod_settings), client=("127.0.0.1", 50000)) as loopback:
        response = loopback.post("/v1/admin/tokens", json={"label": "local-op", "scopes": ["posts:read"]})

    assert response.status_code in {200, 201}, response.text


# ---------------------------------------------------------------------------
# Behavioural: each accepted credential
# ---------------------------------------------------------------------------


def test_bootstrap_admin_token_authenticates(prod_client: TestClient) -> None:
    response = prod_client.post(
        "/v1/admin/tokens",
        json={"label": "operator", "scopes": ["posts:read"]},
        headers={"X-Admin-Token": PROD_ADMIN_TOKEN},
    )

    assert response.status_code in {200, 201}, response.text
    assert response.json()["token"].startswith("acms_")


def test_wrong_bootstrap_admin_token_is_refused(prod_client: TestClient) -> None:
    response = prod_client.post(
        "/v1/admin/tokens",
        json={"label": "attacker", "scopes": ["posts:read"]},
        headers={"X-Admin-Token": "wrong-" + PROD_ADMIN_TOKEN},
    )

    assert response.status_code == 401, response.text


def test_dashboard_session_cookie_reads_the_admin_surface(prod_client: TestClient) -> None:
    """The CSV export link the dashboard renders is a cookie-authenticated GET."""
    prod_client.cookies.set(SESSION_COOKIE, create_session_token("user-1"))

    response = prod_client.get("/v1/admin/audit/export", params={"format": "csv"})

    assert response.status_code == 200, response.text


def _login(client: TestClient) -> str:
    """Attach a valid session + CSRF pair, as the dashboard login sets them."""
    raw = generate_csrf_token()
    client.cookies.set(SESSION_COOKIE, create_session_token("user-1"))
    client.cookies.set(CSRF_COOKIE, f"{raw}|{_sign_csrf(raw)}")
    return raw


def test_dashboard_cookie_cannot_mutate_without_a_csrf_token(prod_client: TestClient) -> None:
    """A bare session cookie on POST is a CSRF target, so it is not enough."""
    _login(prod_client)

    response = prod_client.post("/v1/admin/tokens", json={"label": "csrf", "scopes": ["posts:read"]})

    assert response.status_code == 403, response.text
    assert response.json()["code"] == "csrf-invalid"


def test_dashboard_cookie_with_csrf_token_can_mutate(prod_client: TestClient) -> None:
    raw = _login(prod_client)

    response = prod_client.post(
        "/v1/admin/tokens",
        json={"label": "from-dashboard", "scopes": ["posts:read"]},
        headers={CSRF_TOKEN_HEADER: raw},
    )

    assert response.status_code in {200, 201}, response.text


def _mint(prod_client: TestClient, scopes: list[str]) -> str:
    response = prod_client.post(
        "/v1/admin/tokens",
        json={"label": f"scopes={scopes}", "scopes": scopes},
        headers={"X-Admin-Token": PROD_ADMIN_TOKEN},
    )
    assert response.status_code in {200, 201}, response.text
    return response.json()["token"]


def test_resource_scoped_token_cannot_mint_tokens(prod_client: TestClient) -> None:
    """A site-scoped token is not an admin credential.

    ``scope_for_endpoint()`` returns ``None`` for ``/v1/admin/*``, so before this
    guard *any* valid token -- even a read-only one -- could mint an admin token.
    """
    scoped = _mint(prod_client, ["posts:read", "posts:write"])

    response = prod_client.post(
        "/v1/admin/tokens",
        json={"label": "escalation", "scopes": ["*:read"]},
        headers={"Authorization": f"Bearer {scoped}"},
    )

    assert response.status_code == 403, response.text
    assert response.json()["code"] == "forbidden"


def test_operator_scoped_token_can_mint_tokens(prod_client: TestClient) -> None:
    operator = _mint(prod_client, [ADMIN_SCOPE])

    response = prod_client.post(
        "/v1/admin/tokens",
        json={"label": "machine-operator", "scopes": ["posts:read"]},
        headers={"Authorization": f"Bearer {operator}"},
    )

    assert response.status_code in {200, 201}, response.text


# ---------------------------------------------------------------------------
# Structural: no admin route may mount again without the guard
# ---------------------------------------------------------------------------


def test_every_admin_route_carries_the_guard(prod_client: TestClient) -> None:
    def guarded(route: object) -> bool:
        dependant = getattr(route, "dependant", None)
        if dependant is None:
            return False

        def walk(dependency: object) -> bool:
            if getattr(dependency, "call", None) is require_admin:
                return True
            return any(walk(child) for child in getattr(dependency, "dependencies", ()))

        return any(walk(dep) for dep in dependant.dependencies)

    ungated = sorted(
        route.path
        for route in prod_client.app.routes
        if getattr(route, "path", "").startswith("/v1/admin") and not guarded(route)
    )

    assert not ungated, f"these admin routes mount without require_admin: {ungated}"
