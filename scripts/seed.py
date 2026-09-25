"""Seed the database with demo data (#3, fixed for #28).

Creates: 1 demo site, 3 posts, 1 machine actor + 1 capability link.

The token is minted with the real service helper, so the printed value is the
``cap_<site_slug>_<random>`` shape that ``/c/{token}`` actually accepts --
and the whole script is idempotent: re-running it reuses the demo site/actor
and refreshes the demo link in place instead of exploding on unique slugs.

Usage::

    make seed
    python -m scripts.seed
"""

from __future__ import annotations

import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

# Ensure repo root is on sys.path so imports work when run as a module.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


from app.config import get_settings, reset_settings_cache
from app.db.session import session_scope
from app.models.actor import Actor
from app.models.capability_link import CapabilityLink
from app.models.post import Post
from app.models.post_revision import PostRevision
from app.models.site import Site
from app.services.capability_tokens import ALL_VERBS, generate_capability_token
from sqlalchemy import select
from sqlalchemy.orm import Session

DEMO_ACTOR_LABEL = "Demo API Token"
DEMO_LINK_LABEL = "Demo capability link"
EMBED_LINK_LABEL = "Demo embed link (read-only)"
# GET /embed/v1/posts rejects any token that also carries write verbs, so the demo
# data needs a second, read-only link: the write link can publish drafts but can
# never be embedded.
EMBED_VERBS: tuple[str, ...] = ("posts:read",)
DEMO_POSTS: list[dict[str, str]] = [
    {
        "slug": "hello-world",
        "title": "Hello World",
        "body_md": "# Hello World\n\nThis is the first post on the demo blog.",
        "status": "published",
    },
    {
        "slug": "getting-started",
        "title": "Getting Started with AgentCMS",
        "body_md": "# Getting Started\n\nAgentCMS is a CMS whose first user is an AI agent.",
        "status": "draft",
    },
    {
        "slug": "api-overview",
        "title": "API Overview",
        "body_md": "# API Overview\n\nAll errors are `application/problem+json` with a `hint`.",
        "status": "pending_review",
    },
]


@dataclass(frozen=True)
class SeedResult:
    """The capability tokens ``make seed`` prints, each with a different job.

    ``write_token`` carries posts:read/write/publish and drives ``/c/{token}``.
    ``embed_token`` is read-only on purpose: ``GET /embed/v1/posts`` rejects any
    token that also carries write verbs, so it cannot be the same token.
    """

    write_token: str
    embed_token: str


def _upsert_link(
    session: Session,
    *,
    actor: Actor,
    label: str,
    verbs: Sequence[str],
    slug: str,
    path_scope: str,
) -> str:
    """Mint (or refresh) one capability link and return its plaintext token.

    Minted with the real service helper so the plaintext is the
    ``cap_<site_slug>_<random>`` shape that /c/{token} accepts, with
    sha256(random + secret_key) stored at rest (#28).
    """
    raw_token, token_hash = generate_capability_token(slug)
    link = session.scalar(
        select(CapabilityLink).where(
            CapabilityLink.actor_id == actor.id,
            CapabilityLink.label == label,
        )
    )
    if link is None:
        link = CapabilityLink(id=uuid.uuid4(), actor_id=actor.id, label=label)
        session.add(link)
    link.token_hash = token_hash
    link.site_slug = slug
    link.path_scope = path_scope
    link.verbs = sorted(verbs)
    link.uses_remaining = 1000
    link.uses_count = 0
    link.revoked_at = None
    link.expires_at = None
    return raw_token


def seed() -> SeedResult:
    """Run the seed.  Returns the tokens for the caller to display.

    Idempotent: an existing demo site/actor/post is reused and the demo capability
    links are refreshed in place, so ``make seed`` can be run repeatedly (#28).
    """
    settings = get_settings()
    slug = settings.default_site_slug

    with session_scope() as session:
        # --- site -----------------------------------------------------------------
        site = session.scalar(select(Site).where(Site.slug == slug))
        if site is None:
            site = Site(
                id=uuid.uuid4(),
                slug=slug,
                name="Demo Blog",
                base_url="https://blog.example.com",
                publish_mode=settings.default_publish_mode,
                settings={"description": "A demo site for testing AgentCMS."},
            )
            session.add(site)
            session.flush()

        # --- actor (machine) ------------------------------------------------------
        actor = session.scalar(select(Actor).where(Actor.site_id == site.id, Actor.label == DEMO_ACTOR_LABEL))
        if actor is None:
            actor = Actor(
                id=uuid.uuid4(),
                kind="machine",
                label=DEMO_ACTOR_LABEL,
                site_id=site.id,
                scopes=["posts:read", "posts:write", "posts:publish"],
            )
            session.add(actor)
            session.flush()
        actor.revoked_at = None

        # --- capability links -----------------------------------------------------
        # Two links with deliberately different verbs: the write link drives the
        # publish roundtrip, and the read-only link is what /embed/v1/posts accepts.
        write_token = _upsert_link(
            session,
            actor=actor,
            label=DEMO_LINK_LABEL,
            verbs=sorted(ALL_VERBS),
            slug=slug,
            path_scope=f"/v1/sites/{slug}/posts",
        )
        embed_token = _upsert_link(
            session,
            actor=actor,
            label=EMBED_LINK_LABEL,
            verbs=EMBED_VERBS,
            slug=slug,
            path_scope="/",
        )

        # --- posts ----------------------------------------------------------------
        for data in DEMO_POSTS:
            if session.scalar(select(Post).where(Post.site_id == site.id, Post.slug == data["slug"])):
                continue
            post = Post(
                id=uuid.uuid4(),
                site_id=site.id,
                slug=data["slug"],
                title=data["title"],
                body_md=data["body_md"],
                status=data["status"],
                revision_count=1,
                created_by_actor_id=actor.id,
                author_label="Demo Bot",
            )
            session.add(post)
            session.flush()

            session.add(
                PostRevision(
                    id=uuid.uuid4(),
                    post_id=post.id,
                    revision=1,
                    title=data["title"],
                    body_md=data["body_md"],
                    status=data["status"],
                    editor_label="Demo Bot",
                    actor_id=actor.id,
                )
            )

        return SeedResult(write_token=write_token, embed_token=embed_token)


def main() -> None:
    """CLI entry-point."""
    reset_settings_cache()
    result = seed()
    settings = get_settings()
    slug = settings.default_site_slug
    write_token = result.write_token
    print(f"Seed complete for {settings.app_name}.")
    print(f"  Demo site:         /v1/sites/{slug}")
    print(f"  Capability token:  {write_token}")
    print(f"  Embed token:       {result.embed_token}   (read-only: /embed/v1/posts rejects write tokens)")
    print(f"  Instruction sheet: GET  /c/{write_token}")
    print(f"  Write a draft:     POST /c/{write_token}/posts   (no Authorization header needed)")
    print(f"  Public blog:       GET  /{slug}  ·  /{slug}/rss.xml  ·  /llms.txt")


if __name__ == "__main__":
    main()
