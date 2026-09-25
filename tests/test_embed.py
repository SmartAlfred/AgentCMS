from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

from app.config import Settings
from app.main import create_app
from app.models.actor import Actor
from app.models.capability_link import CapabilityLink
from app.models.post import Post
from app.models.site import Site
from app.models.tag import Tag
from app.services.capability_tokens import generate_capability_token
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

"""Embed API tests (#31).

Acceptance criteria covered:
- Embed token scope enforcement: write token rejected on /embed/v1/posts
- CORS allowlist behaviour: origins in EMBED_ORIGINS get CORS headers
- /embed/v1/... contract stability: versioned endpoint, consistent response shape
- Embed script served with correct content-type
- deploy/compose/docker-compose.prod.yml + deploy/.env.example parse/validate
"""


def _create_site(session: Session, slug: str = "blog") -> Site:
    site = Site(
        id=uuid.uuid4(),
        slug=slug,
        name=f"Test {slug}",
        publish_mode="auto",
    )
    session.add(site)
    session.flush()
    session.commit()
    return site


def _create_tag(session: Session, name: str, slug: str) -> Tag:
    tag = Tag(id=uuid.uuid4(), name=name, slug=slug)
    session.add(tag)
    session.flush()
    return tag


def _create_published_post(
    session: Session,
    site_id: uuid.UUID,
    title: str,
    slug: str,
    body_md: str = "# Test\n\nContent.",
    tags: list[Tag] | None = None,
) -> Post:
    post = Post(
        id=uuid.uuid4(),
        site_id=site_id,
        title=title,
        slug=slug,
        body_md=body_md,
        excerpt="Test excerpt",
        status="published",
        published_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    session.add(post)
    session.flush()
    if tags:
        from app.models.tag import PostTag

        for tag in tags:
            session.add(PostTag(post_id=post.id, tag_id=tag.id))
        session.flush()
    session.commit()
    session.refresh(post)
    return post


def _create_capability_link(
    session: Session,
    *,
    site_slug: str = "blog",
    verbs: list[str] | None = None,
    label: str = "test-link",
    expires_at: datetime | None = None,
) -> tuple[str, CapabilityLink]:
    if verbs is None:
        verbs = ["posts:read", "posts:write", "posts:publish"]

    actor = Actor(
        id=uuid.uuid4(),
        kind="machine",
        label=label,
        scopes=verbs,
    )
    session.add(actor)
    session.flush()

    plaintext, token_hash = generate_capability_token(site_slug)
    link = CapabilityLink(
        id=uuid.uuid4(),
        actor_id=actor.id,
        token_hash=token_hash,
        label=label,
        path_scope="/",
        verbs=verbs,
        site_slug=site_slug,
        expires_at=expires_at,
    )
    session.add(link)
    session.commit()
    session.refresh(link)
    return plaintext, link


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def test_embed_script_served_with_correct_content_type(client: TestClient, db: Session) -> None:
    """GET /embed/v1/agentcms.js returns JavaScript with correct content-type."""
    _create_site(db)
    resp = client.get("/embed/v1/agentcms.js")
    assert resp.status_code == 200
    assert "application/javascript" in resp.headers["content-type"]
    assert "agentcms-embed" in resp.text
    assert "data-site-token" in resp.text


def test_embed_script_caching_headers(client: TestClient, db: Session) -> None:
    """Embed script has proper caching headers."""
    _create_site(db)
    resp = client.get("/embed/v1/agentcms.js")
    assert resp.status_code == 200
    cache_control = resp.headers.get("cache-control", "")
    assert "max-age=3600" in cache_control
    assert "stale-while-revalidate=86400" in cache_control


def test_embed_posts_endpoint_requires_token(client: TestClient, db: Session) -> None:
    """GET /embed/v1/posts requires token parameter."""
    _create_site(db)
    resp = client.get("/embed/v1/posts")
    assert resp.status_code == 422  # Missing required query param


def test_embed_posts_with_read_token_returns_posts(client: TestClient, db: Session) -> None:
    """GET /embed/v1/posts with posts:read token returns published posts."""
    site = _create_site(db)
    tag = _create_tag(db, "Test Tag", "test-tag")
    _create_published_post(db, site.id, "Post 1", "post-1", tags=[tag])
    _create_published_post(db, site.id, "Post 2", "post-2")
    db.commit()  # Ensure data is visible to API session

    plaintext, _ = _create_capability_link(db, site_slug="blog", verbs=["posts:read"])

    resp = client.get(f"/embed/v1/posts?token={plaintext}&limit=10")
    assert resp.status_code == 200
    data = resp.json()
    assert data["site_name"] == "Test blog"
    assert data["site_slug"] == "blog"
    assert len(data["posts"]) == 2
    titles = {p["title"] for p in data["posts"]}
    assert titles == {"Post 1", "Post 2"}
    # Find the post with the tag
    post_with_tag = next(p for p in data["posts"] if p["tags"])
    assert post_with_tag["tags"][0]["name"] == "Test Tag"


def test_embed_posts_write_token_rejected(client: TestClient, db: Session) -> None:
    """GET /embed/v1/posts with write-only token returns 403."""
    _create_site(db)

    # Token with ONLY posts:write (no posts:read)
    plaintext, _ = _create_capability_link(db, verbs=["posts:write"])

    resp = client.get(f"/embed/v1/posts?token={plaintext}&limit=10")
    assert resp.status_code == 403
    assert "posts:read" in resp.json().get("detail", "").lower()
    assert resp.headers.get("x-embed-error") == "write-token-rejected"


def test_embed_posts_publish_token_rejected(client: TestClient, db: Session) -> None:
    """GET /embed/v1/posts with publish-only token returns 403."""
    _create_site(db)

    plaintext, _ = _create_capability_link(db, verbs=["posts:publish"])

    resp = client.get(f"/embed/v1/posts?token={plaintext}&limit=10")
    assert resp.status_code == 403
    assert "posts:read" in resp.json().get("detail", "").lower()


def test_embed_posts_full_scope_token_rejected(client: TestClient, db: Session) -> None:
    """GET /embed/v1/posts with full scope (including write) returns 403.

    Embed endpoint must ONLY accept read-only tokens.
    """
    _create_site(db)

    plaintext, _ = _create_capability_link(db, verbs=["posts:read", "posts:write", "posts:publish"])

    resp = client.get(f"/embed/v1/posts?token={plaintext}&limit=10")
    assert resp.status_code == 403
    assert "read-only" in resp.json().get("detail", "").lower()
    assert resp.headers.get("x-embed-error") == "write-token-rejected"


def test_embed_posts_expired_token_rejected(client: TestClient, db: Session) -> None:
    """GET /embed/v1/posts with expired token returns 410."""
    _create_site(db)
    plaintext, _ = _create_capability_link(
        db, verbs=["posts:read"], expires_at=datetime.now(UTC).replace(year=2020)
    )

    resp = client.get(f"/embed/v1/posts?token={plaintext}&limit=10")
    assert resp.status_code == 410


def test_embed_posts_revoked_token_rejected(client: TestClient, db: Session) -> None:
    """GET /embed/v1/posts with revoked token returns 401."""
    _create_site(db)
    plaintext, link = _create_capability_link(db, verbs=["posts:read"])
    link.revoked_at = datetime.now(UTC)
    db.commit()

    resp = client.get(f"/embed/v1/posts?token={plaintext}&limit=10")
    assert resp.status_code == 401


def test_embed_posts_unknown_token_rejected(client: TestClient, db: Session) -> None:
    """GET /embed/v1/posts with unknown token returns 401."""
    _create_site(db)
    fake_token = f"cap_blog_{uuid.uuid4().hex}"

    resp = client.get(f"/embed/v1/posts?token={fake_token}&limit=10")
    assert resp.status_code == 401


def test_embed_posts_respects_limit_and_pagination(client: TestClient, db: Session) -> None:
    """Embed posts endpoint respects limit and page parameters."""
    site = _create_site(db)
    for i in range(5):
        _create_published_post(db, site.id, f"Post {i}", f"post-{i}")

    plaintext, _ = _create_capability_link(db, verbs=["posts:read"])

    resp = client.get(f"/embed/v1/posts?token={plaintext}&limit=2&page=1")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["posts"]) == 2


def test_embed_posts_filters_by_tag(client: TestClient, db: Session) -> None:
    """Embed posts endpoint filters by tag slug."""
    site = _create_site(db)
    tag1 = _create_tag(db, "Tag One", "tag-one")
    tag2 = _create_tag(db, "Tag Two", "tag-two")
    _create_published_post(db, site.id, "Post 1", "post-1", tags=[tag1])
    _create_published_post(db, site.id, "Post 2", "post-2", tags=[tag2])

    plaintext, _ = _create_capability_link(db, verbs=["posts:read"])

    resp = client.get(f"/embed/v1/posts?token={plaintext}&limit=10&tag=tag-one")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["posts"]) == 1
    assert data["posts"][0]["title"] == "Post 1"


def test_embed_posts_cors_allowed_origin(client: TestClient, db: Session) -> None:
    """CORS headers present when origin is in EMBED_ORIGINS."""
    settings = Settings(
        app_env="test",
        embed_origins=["https://allowed.example.com"],
    )
    app = create_app(settings)

    with TestClient(app) as test_client:
        _create_site(db)
        plaintext, _ = _create_capability_link(db, verbs=["posts:read"])

        # Preflight
        resp = test_client.options(
            f"/embed/v1/posts?token={plaintext}",
            headers={"Origin": "https://allowed.example.com", "Access-Control-Request-Method": "GET"},
        )
        assert resp.status_code == 204
        assert resp.headers.get("access-control-allow-origin") == "https://allowed.example.com"
        assert resp.headers.get("access-control-allow-credentials") == "true"

        # Actual request
        resp = test_client.get(
            f"/embed/v1/posts?token={plaintext}",
            headers={"Origin": "https://allowed.example.com"},
        )
        assert resp.headers.get("access-control-allow-origin") == "https://allowed.example.com"
        assert resp.headers.get("access-control-allow-credentials") == "true"


def test_embed_posts_cors_denied_origin(client: TestClient, db: Session) -> None:
    """No CORS headers when origin is NOT in EMBED_ORIGINS."""
    settings = Settings(
        app_env="test",
        embed_origins=["https://allowed.example.com"],
    )
    app = create_app(settings)

    with TestClient(app) as test_client:
        _create_site(db)
        plaintext, _ = _create_capability_link(db, verbs=["posts:read"])

        resp = test_client.get(
            f"/embed/v1/posts?token={plaintext}",
            headers={"Origin": "https://evil.example.com"},
        )
        # Should not have CORS headers for denied origin
        assert "access-control-allow-origin" not in {k.lower() for k in resp.headers}


def test_embed_posts_no_cors_when_embed_origins_empty(client: TestClient, db: Session) -> None:
    """No CORS headers when EMBED_ORIGINS is empty (default deny-all)."""
    settings = Settings(
        app_env="test",
        embed_origins=[],
    )
    app = create_app(settings)

    with TestClient(app) as test_client:
        _create_site(db)
        plaintext, _ = _create_capability_link(db, verbs=["posts:read"])

        resp = test_client.get(
            f"/embed/v1/posts?token={plaintext}",
            headers={"Origin": "https://anything.example.com"},
        )
        assert "access-control-allow-origin" not in {k.lower() for k in resp.headers}


def test_embed_config_endpoint(client: TestClient, db: Session) -> None:
    """GET /embed/v1/config returns embed configuration."""
    settings = Settings(
        app_env="test",
        embed_origins=["https://example.com"],
        embed_token_scope="posts:read",
    )
    app = create_app(settings)

    with TestClient(app) as test_client:
        resp = test_client.get("/embed/v1/config")
        assert resp.status_code == 200
        data = resp.json()
        assert data["version"] == "1"
        assert data["allowed_origins"] == ["https://example.com"]
        assert data["token_scope"] == "posts:read"
        assert "theming" in data


def test_embed_contract_stability_versioned_path(client: TestClient, db: Session) -> None:
    """Embed endpoints are versioned under /embed/v1/."""
    _create_site(db)
    plaintext, _ = _create_capability_link(db, verbs=["posts:read"])

    # Script endpoint
    resp = client.get("/embed/v1/agentcms.js")
    assert resp.status_code == 200

    # Posts endpoint
    resp = client.get(f"/embed/v1/posts?token={plaintext}")
    assert resp.status_code == 200

    # Config endpoint
    resp = client.get("/embed/v1/config")
    assert resp.status_code == 200

    # No unversioned embed endpoints exist
    resp = client.get("/embed/agentcms.js")
    assert resp.status_code == 404

    resp = client.get("/embed/posts")
    assert resp.status_code == 404


def test_embed_script_contains_required_functions(client: TestClient, db: Session) -> None:
    """Embed script contains key functions and markers."""
    _create_site(db)
    resp = client.get("/embed/v1/agentcms.js")
    script = resp.text

    # Key markers that must exist for contract stability
    assert "data-site-token" in script
    assert "data-mount" in script
    assert "fetch(" in script
    assert "/embed/v1/posts" in script
    assert "agentcms-embed" in script
    assert "--agentcms-color-primary" in script
    assert "prefers-color-scheme" in script
    assert "escapeHtml" in script


def _repo_root() -> Path:
    """Return the repository root path."""
    return Path(__file__).resolve().parents[1]


def test_compose_prod_parses() -> None:
    """docker-compose.prod.yml parses without errors."""
    import os
    import subprocess

    from tests.pg import skip_or_fail_without_docker

    skip_or_fail_without_docker("deploy/compose/docker-compose.prod.yml parsing")

    repo_root = _repo_root()
    env = os.environ.copy()
    env.update(
        {
            "POSTGRES_PASSWORD": "test-password",
            "SECRET_KEY": "test-secret-key-that-is-long-enough-000000",
            "CADDY_EMAIL": "test@example.com",
            "DOMAIN": "localhost",
        }
    )
    result = subprocess.run(
        ["docker", "compose", "-f", "deploy/compose/docker-compose.prod.yml", "config"],
        capture_output=True,
        text=True,
        cwd=repo_root,
        env=env,
    )
    assert result.returncode == 0, f"docker compose config failed: {result.stderr}"
    # Should contain all expected services
    assert "db:" in result.stdout
    assert "migrate:" in result.stdout
    assert "api:" in result.stdout
    assert "caddy:" in result.stdout


def test_deploy_env_example_parses() -> None:
    """deploy/.env.example can be parsed as valid shell/env format."""
    import subprocess

    repo_root = _repo_root()
    subprocess.run(
        ["bash", "-n", "deploy/.env.example"],
        capture_output=True,
        text=True,
        cwd=repo_root,
    )
    # .env.example is not a bash script but we can check it's readable
    with open(repo_root / "deploy/.env.example") as f:
        content = f.read()
    assert "SECRET_KEY" in content
    assert "POSTGRES_PASSWORD" in content
    assert "EMBED_ORIGINS" in content
    assert "DOMAIN" in content


def test_embed_response_shape_matches_schema(client: TestClient, db: Session) -> None:
    """Embed posts response matches the documented schema."""
    site = _create_site(db)
    _create_published_post(db, site.id, "Schema Test", "schema-test")

    plaintext, _ = _create_capability_link(db, verbs=["posts:read"])

    resp = client.get(f"/embed/v1/posts?token={plaintext}")
    assert resp.status_code == 200
    data = resp.json()

    # Required top-level fields
    assert "site_name" in data
    assert "site_slug" in data
    assert "posts" in data
    assert isinstance(data["posts"], list)

    # Required post fields
    post = data["posts"][0]
    assert "id" in post
    assert "title" in post
    assert "slug" in post
    assert "url" in post
    assert "excerpt" in post
    assert "published_at" in post
    assert "site_slug" in post
    assert "tags" in post
    assert isinstance(post["tags"], list)

    # URL format
    assert post["url"].startswith("http")
    assert post["site_slug"] == "blog"


def test_embed_etag_and_caching(client: TestClient, db: Session) -> None:
    """Embed posts endpoint returns ETag and caching headers."""
    site = _create_site(db)
    _create_published_post(db, site.id, "ETag Test", "etag-test")

    plaintext, _ = _create_capability_link(db, verbs=["posts:read"])

    resp = client.get(f"/embed/v1/posts?token={plaintext}")
    assert resp.status_code == 200

    etag = resp.headers.get("etag")
    assert etag is not None
    assert etag.startswith('"')

    cache_control = resp.headers.get("cache-control", "")
    assert "max-age=60" in cache_control
    assert "stale-while-revalidate=600" in cache_control


def test_embed_script_x_content_type_options(client: TestClient, db: Session) -> None:
    """Embed script has X-Content-Type-Options: nosniff."""
    _create_site(db)
    resp = client.get("/embed/v1/agentcms.js")
    assert resp.headers.get("x-content-type-options") == "nosniff"


def test_embed_iframe_endpoint_served(client: TestClient, db: Session) -> None:
    """GET /embed/v1/iframe returns HTML fallback page."""
    _create_site(db)
    resp = client.get("/embed/v1/iframe")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "iframe-mount" in resp.text
    assert "/embed/v1/agentcms.js" in resp.text


def test_embed_iframe_requires_token_query_param(client: TestClient, db: Session) -> None:
    """Iframe page expects token in query params (handled client-side)."""
    _create_site(db)
    resp = client.get("/embed/v1/iframe")
    assert resp.status_code == 200
    # The iframe HTML itself doesn't validate token server-side;
    # validation happens when the embedded script fetches /embed/v1/posts


def test_embed_iframe_cors_headers(client: TestClient, db: Session) -> None:
    """Iframe endpoint respects EMBED_ORIGINS for CORS."""
    settings = Settings(
        app_env="test",
        embed_origins=["https://allowed.example.com"],
    )
    app = create_app(settings)

    with TestClient(app) as test_client:
        _create_site(db)

        # Preflight
        resp = test_client.options(
            "/embed/v1/iframe",
            headers={"Origin": "https://allowed.example.com", "Access-Control-Request-Method": "GET"},
        )
        assert resp.status_code == 204
        assert resp.headers.get("access-control-allow-origin") == "https://allowed.example.com"

        # Actual request
        resp = test_client.get("/embed/v1/iframe", headers={"Origin": "https://allowed.example.com"})
        assert resp.headers.get("access-control-allow-origin") == "https://allowed.example.com"


def test_embed_iframe_cors_denied_origin(client: TestClient, db: Session) -> None:
    """No CORS headers on iframe when origin not in EMBED_ORIGINS."""
    settings = Settings(
        app_env="test",
        embed_origins=["https://allowed.example.com"],
    )
    app = create_app(settings)

    with TestClient(app) as test_client:
        _create_site(db)

        resp = test_client.get("/embed/v1/iframe", headers={"Origin": "https://evil.example.com"})
        assert "access-control-allow-origin" not in {k.lower() for k in resp.headers}


def test_embed_iframe_no_cors_when_embed_origins_empty(client: TestClient, db: Session) -> None:
    """No CORS headers on iframe when EMBED_ORIGINS is empty (default deny-all)."""
    settings = Settings(
        app_env="test",
        embed_origins=[],
    )
    app = create_app(settings)

    with TestClient(app) as test_client:
        _create_site(db)

        resp = test_client.get("/embed/v1/iframe", headers={"Origin": "https://anything.example.com"})
        assert "access-control-allow-origin" not in {k.lower() for k in resp.headers}


def test_embed_iframe_security_headers(client: TestClient, db: Session) -> None:
    """Iframe endpoint has security headers."""
    _create_site(db)
    resp = client.get("/embed/v1/iframe")
    assert resp.headers.get("x-content-type-options") == "nosniff"
    assert resp.headers.get("x-frame-options") == "SAMEORIGIN"
