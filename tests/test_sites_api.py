"""Regression tests for the site API (#45).

The quickstart, ``make selfhost-verify`` and the #37 self-host E2E job all ask
for ``POST /v1/sites``, which did not exist — every one of them 404'd on a real
stack, and the only way to get a site was ``scripts/seed.py`` or SQL.  Each test
below fails on the pre-#45 tree and passes after it.
"""

from __future__ import annotations

import uuid

import pytest
from app.models.actor import Actor
from app.models.capability_link import CapabilityLink
from app.services.tokens import generate_token
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

SITE = {"slug": "docs", "name": "Docs Site"}


def _token_headers(db: Session, scopes: list[str]) -> dict[str, str]:
    """A real token with exactly ``scopes`` (verbs wide open, so a rejection
    here can only be about scopes, never about the HTTP verb)."""
    actor = Actor(id=uuid.uuid4(), kind="machine", label="sites-test", scopes=scopes)
    db.add(actor)
    db.flush()
    plaintext, token_hash = generate_token(actor.id)
    db.add(
        CapabilityLink(
            id=uuid.uuid4(),
            actor_id=actor.id,
            token_hash=token_hash,
            label="sites-test",
            path_scope="/",
            verbs=["GET", "POST", "PATCH", "DELETE"],
        )
    )
    db.commit()
    return {"Authorization": f"Bearer {plaintext}"}


def test_create_site_returns_a_usable_site(client: TestClient, auth_headers) -> None:
    resp = client.post("/v1/sites", json=SITE, headers=auth_headers)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["slug"] == "docs"
    assert body["name"] == "Docs Site"
    assert body["publish_mode"] == "auto"
    assert body["created_at"]
    uuid.UUID(body["id"])  # a real id, not a placeholder


def test_the_smoke_scripts_flow_create_then_read_back_works(client: TestClient, auth_headers) -> None:
    """`deploy_smoke.sh` creates the site and falls back to GET on a re-run."""
    created = client.post("/v1/sites", json={"slug": "blog", "name": "Smoke Test Blog"}, headers=auth_headers)
    assert created.status_code == 201, created.text
    fetched = client.get("/v1/sites/blog", headers=auth_headers)
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["id"] == created.json()["id"]


def test_duplicate_slug_is_a_clean_409_and_never_an_overwrite(client: TestClient, auth_headers) -> None:
    assert client.post("/v1/sites", json=SITE, headers=auth_headers).status_code == 201
    again = client.post("/v1/sites", json=SITE, headers=auth_headers)
    assert again.status_code == 409, again.text
    assert again.json()["code"] == "site-slug-conflict"
    assert client.get("/v1/sites", headers=auth_headers).json()["count"] == 1


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"slug": "Docs Site", "name": "Docs"}, "slug"),
        ({"slug": "docs", "name": "   "}, "name"),
        ({"slug": "docs", "name": "Docs", "publish_mode": "yolo"}, "publish_mode"),
    ],
)
def test_invalid_site_payloads_are_422(client: TestClient, auth_headers, body: dict, field: str) -> None:
    resp = client.post("/v1/sites", json=body, headers=auth_headers)
    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "invalid-site"
    assert field in resp.text


def test_creating_a_site_requires_the_write_scope(client: TestClient, db: Session, auth_headers) -> None:
    headers = _token_headers(db, scopes=["sites:read"])
    resp = client.post("/v1/sites", json=SITE, headers=headers)
    assert resp.status_code == 403, resp.text
    assert "sites:write" in resp.text
    assert client.get("/v1/sites", headers=auth_headers).json()["count"] == 0


def test_anonymous_requests_are_rejected(client: TestClient) -> None:
    assert client.post("/v1/sites", json=SITE).status_code == 401
    assert client.get("/v1/sites").status_code == 401


def test_unknown_site_is_404(client: TestClient, auth_headers) -> None:
    resp = client.get("/v1/sites/nope", headers=auth_headers)
    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == "site-not-found"


def test_list_sites_is_deterministic(client: TestClient, auth_headers) -> None:
    for slug in ("alpha", "beta"):
        assert (
            client.post(
                "/v1/sites", json={"slug": slug, "name": slug.title()}, headers=auth_headers
            ).status_code
            == 201
        )
    listed = client.get("/v1/sites", headers=auth_headers).json()
    assert listed["count"] == 2
    assert [item["slug"] for item in listed["items"]] == ["alpha", "beta"]
