"""Seed the database with demo data (#3).

Creates: 1 demo site, 3 posts, 1 API token actor + capability link.

Usage::

    make seed
    python -m scripts.seed
"""

from __future__ import annotations

import hashlib
import secrets
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


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def seed() -> str:
    """Run the seed.  Returns the raw token for the caller to display."""
    settings = get_settings()

    with session_scope() as session:
        # --- site -----------------------------------------------------------------
        site = Site(
            id=uuid.uuid4(),
            slug=settings.default_site_slug,
            name="Demo Blog",
            base_url="https://blog.example.com",
            publish_mode=settings.default_publish_mode,
            settings={"description": "A demo site for testing AgentCMS."},
        )
        session.add(site)
        session.flush()

        # --- actor (machine) ------------------------------------------------------
        actor = Actor(
            id=uuid.uuid4(),
            kind="machine",
            label="Demo API Token",
            site_id=site.id,
            scopes=["posts:read", "posts:write", "posts:publish"],
        )
        session.add(actor)
        session.flush()

        # --- capability link ------------------------------------------------------
        raw_token = secrets.token_urlsafe(32)
        token_hash = _hash_token(raw_token)
        cap_link = CapabilityLink(
            id=uuid.uuid4(),
            actor_id=actor.id,
            token_hash=token_hash,
            path_scope="/v1/sites/blog/posts",
            verbs=["GET", "POST", "PATCH"],
            uses_remaining=1000,
        )
        session.add(cap_link)

        # --- posts ----------------------------------------------------------------
        posts_data = [
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
        for data in posts_data:
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

            rev = PostRevision(
                id=uuid.uuid4(),
                post_id=post.id,
                revision=1,
                title=data["title"],
                body_md=data["body_md"],
                status=data["status"],
                editor_label="Demo Bot",
                actor_id=actor.id,
            )
            session.add(rev)

    return raw_token


def main() -> None:
    """CLI entry-point."""
    reset_settings_cache()
    token = seed()
    settings = get_settings()
    print(f"Seed complete for {settings.app_name}.")
    print(f"  Demo site:     /v1/sites/{settings.default_site_slug}")
    print(f"  API token:     {token}")
    print("  Use:  Authorization: Bearer <token>")


if __name__ == "__main__":
    main()
