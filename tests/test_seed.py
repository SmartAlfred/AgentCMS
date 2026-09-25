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
    token = seed().write_token

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
    token = seed().write_token

    sheet = client.get(f"/c/{token}")
    assert sheet.status_code == 200, sheet.text

    created = client.post(f"/c/{token}/posts", json={"title": "Written by an agent", "body_md": "# Hello"})
    assert created.status_code == 201, created.text
    assert created.json()["status"] == "draft"


def test_seed_is_idempotent_and_refreshes_the_demo_links(db: Session) -> None:
    # Two links on purpose: the write/publish link and the read-only embed link.
    first = seed()
    before = _counts(db)

    second = seed()
    after = _counts(db)

    assert before == after == {"sites": 1, "actors": 1, "links": 2, "posts": 3}
    assert second.write_token != first.write_token, "a re-run should mint a fresh token"
    assert second.embed_token != first.embed_token, "a re-run should refresh the embed link too"
    # The refreshed token works and the old one no longer resolves.
    verify_capability_token(db, second.write_token, required_verb="posts:write", required_site_slug="blog")
    assert (
        db.scalar(
            select(CapabilityLink).where(CapabilityLink.token_hash == first.write_token.split("_", 2)[-1])
        )
        is None
    )


def test_seeded_embed_token_is_read_only_and_the_write_token_is_rejected(
    db: Session, client: TestClient
) -> None:
    """``make seed`` must mint a second, read-only link for embedding.

    The embed surface rejects any token carrying write verbs, so a single
    write-capable token (the pre-fix behaviour) made the documented embed setup and
    the self-host E2E's final assertion fail with 403 on a perfectly healthy stack --
    which is how the self-host E2E job went red on 2026-09-25.
    """
    result = seed()

    embed = client.get(f"/embed/v1/posts?token={result.embed_token}&limit=10")
    assert embed.status_code == 200, embed.text
    assert embed.json()["posts"], "the seeded published post must come back"

    write = client.get(f"/embed/v1/posts?token={result.write_token}&limit=10")
    assert write.status_code == 403, write.text
    assert "read-only" in write.json().get("detail", "").lower()

    # The documented way to reach the embed link is the printed "Embed token:" line.
    _, link = verify_capability_token(
        db, result.embed_token, required_verb="posts:read", required_site_slug="blog"
    )
    assert set(link.verbs or []) == {"posts:read"}, "the embed link must be read-only"
