"""The human dashboard must be reachable, and the public catch-alls must not eat it (#42).

Regression test for a P1: ``register_public_routes`` ran *before*
``register_dashboard_routes``, so the public ``/{site_slug}/{slug}`` catch-all
swallowed every ``/dashboard/...`` URL (``/dashboard/login`` matched
site_slug="dashboard", slug="login") and served a 404. ``/dashboard/`` itself
redirected straight into that 404, so the whole dashboard 404'd in every
deployment. Nothing caught it because no test touched a dashboard route.

Starlette matches in registration order, so both halves of the contract are
tested: the URLs a human types, and the registration order that decides them.
"""

from __future__ import annotations

import inspect

import pytest
from app.main import create_app
from fastapi.testclient import TestClient

DASHBOARD_PAGES = (
    "/dashboard/",
    "/dashboard/posts",
    "/dashboard/reviews",
    "/dashboard/tokens",
    "/dashboard/activity",
    "/dashboard/settings",
)


def test_login_page_is_served_not_swallowed_by_the_public_catch_all(
    client: TestClient,
) -> None:
    response = client.get("/dashboard/login")

    assert response.status_code == 200, "the dashboard login page must be reachable"
    assert response.headers["content-type"].startswith("text/html")
    assert "login" in response.text.lower()


@pytest.mark.parametrize("path", DASHBOARD_PAGES)
def test_unauthenticated_pages_redirect_to_login(client: TestClient, path: str) -> None:
    """302 to /dashboard/login — not the public handler's 404."""

    response = client.get(path, follow_redirects=False)

    assert response.status_code == 302, f"{path} is not being served by the dashboard router"
    assert response.headers["location"] == "/dashboard/login"


def test_dashboard_routes_are_registered_before_the_public_catch_alls() -> None:
    """The root cause, asserted directly: call order in create_app.

    Starlette matches in registration order, so this is the invariant that
    decides whether /dashboard/... is reachable. FastAPI 0.141 keeps included
    routers unflattened, so the order is read from the wiring itself rather
    than from ``app.routes``.
    """

    source = inspect.getsource(create_app)
    dashboard = source.index("register_dashboard_routes(app)")
    public = source.index("register_public_routes(app)")

    assert dashboard < public, "public catch-alls must be mounted after the dashboard"


def test_public_catch_alls_still_work_behind_the_dashboard(client: TestClient) -> None:
    """Moving the dashboard in front must not break the public read surface."""

    post = client.get("/no-such-site/no-such-post")
    assert post.status_code == 404
    assert post.headers["content-type"] == "application/problem+json"

    site = client.get("/no-such-site")
    assert site.status_code == 404
    assert site.headers["content-type"] == "application/problem+json"


def test_static_public_files_are_untouched(client: TestClient) -> None:
    assert client.get("/robots.txt").status_code == 200
