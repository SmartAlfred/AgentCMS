"""Regression tests for #28 — ``make seed`` must mint a token that really authenticates.

The seed used to write the pre-#6 shape (no ``cap_<site>_`` prefix, unpeppered hash,
NULL ``site_slug``, HTTP verbs instead of scopes), so the token it printed was rejected
with 401 "Malformed capability token." on every documented path.
"""

from __future__ import annotations

from app.models.actor import Actor
from app.models.capability_link import CapabilityLink
from app.models.post import Post
from app.models.site import Site
from app.services.capability_tokens import verify_capability_token
from scripts.seed import seed
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.testclient import TestClient


def _counts(db: Session) -> dict[str, int]:
    return {
        "sites": db.scalar(select(func.count()).select_from(Site)) or 0,
        "actors": db.scalar(select(func.count()).select_from(Actor)) or 0,
        "links": db.scalar(select(func.count()).select_from(CapabilityLink)) or 0,
        "posts": db.scalar(select(func.count()).select_from(Post)) or 0,
    }


def test_seed_token_verifies_with_the_real_capability_service(db: Session) -> None:
    token, _ = seed()

    assert token.startswith("cap_blog_"), f"seeded token has the pre-#6 shape: {token!r}"

    actor, link = verify_capability_token(
        db,
        token,
        required_verb="posts:write",
        required_site_slug="blog",
    )
    assert actor.kind == "machine"
    assert link.site_slug == "blog"
    assert set(link.verbs or []) >= {"posts:read", "posts:write", "posts:publish"}
    assert "GET" not in (link.verbs or []), "verbs must be scopes, not HTTP verbs"


def test_seeded_token_is_accepted_by_the_instruction_sheet_and_write_path(
    db: Session, client: TestClient
) -> None:
    token, _ = seed()

    sheet = client.get(f"/c/{token}")
    assert sheet.status_code == 200, sheet.text

    created = client.post(f"/c/{token}/posts", json={"title": "Written by an agent", "body_md": "# Hello"})
    assert created.status_code == 201, created.text
    assert created.json()["status"] == "draft"


def test_seed_is_idempotent_and_refreshes_the_demo_link(db: Session) -> None:
    first, _ = seed()
    before = _counts(db)

    second, _ = seed()
    after = _counts(db)

    # Idempotent: a re-run must not add rows.  Two links by design -- the write
    # token for /c/{token} and the read-only token the embed surface accepts (#37).
    assert before == after, "a re-run must not create rows"
    assert before["sites"] == before["actors"] == 1
    assert before["links"] == 2, "one write link and one read-only embed link"
    assert db.scalar(select(CapabilityLink).where(CapabilityLink.label == "demo-embed-read-only")) is not None
    assert second != first, "a re-run should mint a fresh token"
    # The refreshed token works and the old one no longer resolves.
    verify_capability_token(db, second, required_verb="posts:write", required_site_slug="blog")
    assert (
        db.scalar(select(CapabilityLink).where(CapabilityLink.token_hash == first.split("_", 2)[-1])) is None
    )
