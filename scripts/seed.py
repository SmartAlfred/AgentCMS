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

DEMO_ACTOR_LABEL = "Demo API Token"
DEMO_LINK_LABEL = "Demo capability link"
DEMO_EMBED_LINK_LABEL = "demo-embed-read-only"
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


def seed() -> tuple[str, str]:
    """Run the seed.  Returns the raw capability token for the caller to display.

    Idempotent: an existing demo site/actor/post is reused and the demo capability
    link is refreshed in place, so ``make seed`` can be run repeatedly (#28).
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

        # --- capability link ------------------------------------------------------
        # Minted with the real service helper so the plaintext is the
        # ``cap_<site_slug>_<random>`` shape that /c/{token} accepts, with
        # sha256(random + secret_key) stored at rest (#28).
        raw_token, token_hash = generate_capability_token(slug)
        link = session.scalar(
            select(CapabilityLink).where(
                CapabilityLink.actor_id == actor.id,
                CapabilityLink.label == DEMO_LINK_LABEL,
            )
        )
        if link is None:
            link = CapabilityLink(id=uuid.uuid4(), actor_id=actor.id, label=DEMO_LINK_LABEL)
            session.add(link)
        link.token_hash = token_hash
        link.site_slug = slug
        link.path_scope = f"/v1/sites/{slug}/posts"
        link.verbs = sorted(ALL_VERBS)
        link.uses_remaining = 1000
        link.uses_count = 0
        link.revoked_at = None
        link.expires_at = None

        # --- embed capability link (read-only, #37) -------------------------------
        # ``/embed/v1/posts`` refuses any token whose verbs include posts:write or
        # posts:publish (app/api/embed/routes.py) and docs/deploy/embed.md tells
        # self-hosters to use a "posts:read scope only" token -- so the seed has to
        # issue one, or the documented embed path is unreachable without hand-written
        # SQL.  The write token above keeps its scope for the /c/{token} agent flow.
        embed_raw, embed_hash = generate_capability_token(slug)
        embed_link = session.scalar(
            select(CapabilityLink).where(
                CapabilityLink.actor_id == actor.id,
                CapabilityLink.label == DEMO_EMBED_LINK_LABEL,
            )
        )
        if embed_link is None:
            embed_link = CapabilityLink(id=uuid.uuid4(), actor_id=actor.id, label=DEMO_EMBED_LINK_LABEL)
            session.add(embed_link)
        embed_link.token_hash = embed_hash
        embed_link.site_slug = slug
        embed_link.path_scope = f"/v1/sites/{slug}/posts"
        embed_link.verbs = ["posts:read"]
        embed_link.uses_remaining = 1000
        embed_link.uses_count = 0
        embed_link.revoked_at = None
        embed_link.expires_at = None

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

    return raw_token, embed_raw


def main() -> None:
    """CLI entry-point."""
    reset_settings_cache()
    token, embed_token = seed()
    settings = get_settings()
    slug = settings.default_site_slug
    print(f"Seed complete for {settings.app_name}.")
    print(f"  Demo site:         /v1/sites/{slug}")
    print(f"  Capability token:  {token}")
    print(f"  Embed token (read-only): {embed_token}")
    print("    (use this one for /embed/v1/posts?token=... -- write tokens are rejected)")
    print(f"  Instruction sheet: GET  /c/{token}")
    print(f"  Write a draft:     POST /c/{token}/posts   (no Authorization header needed)")
    print(f"  Public blog:       GET  /{slug}  ·  /{slug}/rss.xml  ·  /llms.txt")


if __name__ == "__main__":
    main()
