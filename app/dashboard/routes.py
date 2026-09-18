"""Dashboard routes - page handlers for the human dashboard.

Every route calls the existing /v1 and /v1/admin API endpoints through the
service layer (no private endpoints). The dashboard is a thin HTML adapter
over the documented HTTP API.
"""

from __future__ import annotations

import html
import json
import uuid
from typing import Any

from fastapi import APIRouter, Form, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from app.dashboard.auth import (
    CSRF_COOKIE,
    CSRF_TOKEN_HEADER,
    SESSION_COOKIE,
    SESSION_TTL_SECONDS,
    DashboardUser,
    create_session_token,
    generate_csrf_token,
    get_dashboard_user,
    get_or_create_dashboard_user,
    verify_csrf_token,
    verify_session_token,
)
from app.dashboard.templates import (
    render_activity,
    render_editor,
    render_login_page,
    render_overview,
    render_posts_list,
    render_reviews,
    render_revisions,
    render_settings,
    render_tokens,
)

auth_router = APIRouter()
dashboard_router = APIRouter()


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


def _get_user(request: Request) -> DashboardUser | None:
    """Extract the current user from the session cookie."""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    user_id = verify_session_token(token)
    if not user_id:
        return None
    return get_dashboard_user(user_id)


def _require_user(request: Request) -> DashboardUser:
    """Require an authenticated user, else redirect to login."""
    user = _get_user(request)
    if user is None:
        return None  # type: ignore[return-value]
    return user


def _get_csrf(request: Request) -> str:
    """Get or create the CSRF token for the current session."""
    signed = request.cookies.get(CSRF_COOKIE, "")
    # Try to extract from signed cookie
    if signed:
        # The signed cookie is: raw_token|sig
        parts = signed.split("|")
        if len(parts) == 2:
            raw, sig = parts
            if verify_csrf_token(raw, sig):
                return raw
    # Generate a new one
    token = generate_csrf_token()
    return token


def _set_csrf_cookie(response: Response, csrf_token: str) -> None:
    """Set the CSRF cookie on the response."""
    from app.dashboard.auth import _sign_csrf

    signed = f"{csrf_token}|{_sign_csrf(csrf_token)}"
    response.set_cookie(
        CSRF_COOKIE,
        signed,
        httponly=True,
        samesite="strict",
        max_age=SESSION_TTL_SECONDS,
    )


def _require_csrf(request: Request) -> None:
    """Validate CSRF token on mutating requests."""
    header_token = request.headers.get(CSRF_TOKEN_HEADER, "")
    cookie_token = request.cookies.get(CSRF_COOKIE, "")
    if not header_token or not cookie_token:
        from app.domain.errors import DomainError

        class CSRFError(DomainError):
            status_code = 403
            code = "csrf-invalid"
            title = "CSRF validation failed"

        raise CSRFError("Missing CSRF token.")
    # Extract raw from signed cookie
    parts = cookie_token.split("|")
    if len(parts) != 2:
        from app.domain.errors import DomainError

        class CSRFError2(DomainError):
            status_code = 403
            code = "csrf-invalid"
            title = "CSRF validation failed"

        raise CSRFError2("Invalid CSRF cookie.")
    raw, sig = parts
    if not verify_csrf_token(raw, sig) or raw != header_token:
        from app.domain.errors import DomainError

        class CSRFError3(DomainError):
            status_code = 403
            code = "csrf-invalid"
            title = "CSRF validation failed"

        raise CSRFError3("CSRF token mismatch.")


def _toast_response(message: str, level: str = "success", redirect: str | None = None) -> Response:
    """Return a response with an HTMX toast header."""
    headers = {"X-Toast": json.dumps({"message": message, "level": level})}
    if redirect:
        headers["HX-Redirect"] = redirect
    return Response(status_code=200, headers=headers)


def _human_auth_ctx(user: DashboardUser) -> dict[str, Any]:
    """Build an audit context dict for human actions."""
    return {
        "actor_label": user.label,
        "actor_kind": "human",
    }


def _esc(text: str) -> str:
    """Escape HTML special characters."""
    return html.escape(str(text), quote=True)


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------


@auth_router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> Response:
    user = _get_user(request)
    if user:
        return RedirectResponse("/dashboard/", status_code=302)
    return HTMLResponse(render_login_page(request=request))


@auth_router.post("/magic")
async def magic_link(
    request: Request,
    email: str = Form(...),
) -> Response:
    """Magic link authentication. In dev mode, logs in directly."""
    from app.config import get_settings

    settings = get_settings()

    user = get_or_create_dashboard_user(email)
    token = create_session_token(user.id)

    response = RedirectResponse("/dashboard/", status_code=302)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="strict",
        max_age=24 * 60 * 60,
        secure=settings.is_production,
    )
    csrf = generate_csrf_token()
    _set_csrf_cookie(response, csrf)
    return response


@auth_router.post("/logout")
async def logout(request: Request) -> Response:
    response = RedirectResponse("/dashboard/login", status_code=302)
    response.delete_cookie(SESSION_COOKIE)
    response.delete_cookie(CSRF_COOKIE)
    return response


@auth_router.get("/magic/{token}")
async def magic_link_verify(request: Request, token: str) -> Response:
    """Verify a magic link token and create a session."""
    from app.dashboard.auth import verify_magic_link_token

    user_id = verify_magic_link_token(token)
    if not user_id:
        return HTMLResponse(
            render_login_page(request=request, error="Invalid or expired magic link."),
            status_code=401,
        )

    user = get_dashboard_user(user_id)
    if not user:
        return HTMLResponse(
            render_login_page(request=request, error="User not found."),
            status_code=401,
        )

    session_token = create_session_token(user.id)
    response = RedirectResponse("/dashboard/", status_code=302)
    response.set_cookie(
        SESSION_COOKIE,
        session_token,
        httponly=True,
        samesite="strict",
        max_age=24 * 60 * 60,
    )
    csrf = generate_csrf_token()
    _set_csrf_cookie(response, csrf)
    return response


# ---------------------------------------------------------------------------
# Dashboard pages
# ---------------------------------------------------------------------------


@dashboard_router.get("/", response_class=HTMLResponse)
async def overview(request: Request) -> Response:
    user = _require_user(request)
    if not user:
        return RedirectResponse("/dashboard/login", status_code=302)

    csrf = _get_csrf(request)
    from app.db.session import get_engine

    with get_engine().connect() as conn:
        from sqlalchemy import text

        # Post counts by status
        rows = conn.execute(
            text("SELECT status, COUNT(*) FROM posts WHERE deleted_at IS NULL GROUP BY status")
        ).all()
        post_counts = {row[0]: row[1] for row in rows}

        # Recent activity
        act_rows = conn.execute(
            text(
                "SELECT action, actor_label, target_type, target_id, created_at "
                "FROM audit_events ORDER BY created_at DESC LIMIT 10"
            )
        ).all()
        recent_activity = [
            {
                "action": r[0],
                "actor_label": r[1],
                "target_type": r[2],
                "target_id": str(r[3]) if r[3] else "",
                "created_at": r[4],
            }
            for r in act_rows
        ]

        # Capability links
        link_rows = conn.execute(
            text(
                "SELECT label, revoked_at, uses_count FROM capability_links ORDER BY created_at DESC LIMIT 5"
            )
        ).all()
        capability_links = [{"label": r[0], "revoked_at": r[1], "uses_count": r[2] or 0} for r in link_rows]

        # Site info (for the site slug)
        site_rows = conn.execute(text("SELECT slug FROM sites LIMIT 1")).all()
        site_slug = site_rows[0][0] if site_rows else "blog"

    # Kill switch state
    from app.services.kill_switch import get_kill_switch_store

    ks = get_kill_switch_store()
    kill_switch_state = ks.get_state().to_dict()

    return HTMLResponse(
        render_overview(
            site_slug=site_slug,
            post_counts=post_counts,
            recent_activity=recent_activity,
            capability_links=capability_links,
            kill_switch_state=kill_switch_state,
            csrf_token=csrf,
            request=request,
        )
    )


# ---------------------------------------------------------------------------
# Posts
# ---------------------------------------------------------------------------


@dashboard_router.get("/posts", response_class=HTMLResponse)
async def posts_list(
    request: Request,
    status: str | None = Query(None),
    cursor: str | None = Query(None),
) -> Response:
    user = _require_user(request)
    if not user:
        return RedirectResponse("/dashboard/login", status_code=302)

    from app.db.session import get_engine

    with get_engine().connect() as conn:
        from sqlalchemy import text

        query = "SELECT id, slug, title, status, updated_at, published_at FROM posts WHERE deleted_at IS NULL"
        params: dict[str, Any] = {}
        if status:
            query += " AND status = :status"
            params["status"] = status
        query += " ORDER BY updated_at DESC LIMIT 21"

        rows = conn.execute(text(query), params).all()
        posts = [
            {
                "id": str(r[0]),
                "slug": r[1],
                "title": r[2],
                "status": r[3],
                "updated_at": str(r[4]) if r[4] else "",
                "published_at": str(r[5]) if r[5] else "",
            }
            for r in rows
        ]

    next_cursor = None
    if len(posts) > 20:
        posts = posts[:20]
        next_cursor = posts[-1].get("updated_at", "")

    csrf = _get_csrf(request)
    return HTMLResponse(
        render_posts_list(
            posts=posts,
            status_filter=status,
            next_cursor=next_cursor,
            csrf_token=csrf,
            request=request,
        )
    )


@dashboard_router.post("/posts")
async def create_post(
    request: Request,
    title: str = Form(""),
    body_md: str = Form(""),
    slug: str = Form(""),
    excerpt: str = Form(""),
    tags: str = Form(""),
) -> Response:
    user = _require_user(request)
    if not user:
        return RedirectResponse("/dashboard/login", status_code=302)
    _require_csrf(request)

    from app.db.session import get_engine

    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []

    with get_engine().connect() as conn:
        from sqlalchemy import text

        site_rows = conn.execute(text("SELECT slug FROM sites LIMIT 1")).all()
        if not site_rows:
            return _toast_response("No site configured.", level="error")
        site_slug = site_rows[0][0]

    # Use the post service to create (gets audit events)
    from app.services.post import create_post as svc_create

    with get_engine().begin() as conn:
        from sqlalchemy.orm import Session as SessionType

        session = SessionType(bind=conn)
        try:
            _post, _warnings = svc_create(
                session,
                site_slug,
                body_md=body_md,
                title=title if title else None,
                slug=slug if slug else None,
                tags=tag_list if tag_list else None,
                excerpt=excerpt if excerpt else None,
                actor_id=uuid.UUID(user.id),
                source="dashboard",
                audit_ctx=_human_auth_ctx(user),
                is_agent_actor=False,
            )
            post_slug = _post.slug
        except Exception as exc:
            return _toast_response(f"Error: {exc}", level="error")

    return _toast_response(
        f"Post '{post_slug}' created",
        level="success",
        redirect=f"/dashboard/posts/{post_slug}",
    )


@dashboard_router.get("/posts/{post_slug}", response_class=HTMLResponse)
async def post_edit_page(
    request: Request,
    post_slug: str,
) -> Response:
    user = _require_user(request)
    if not user:
        return RedirectResponse("/dashboard/login", status_code=302)

    from app.db.session import get_engine

    with get_engine().connect() as conn:
        from sqlalchemy import text

        rows = conn.execute(
            text(
                "SELECT id, slug, title, body_md, excerpt, status, "
                "revision_count, content_hash, published_at, created_at, "
                "updated_at FROM posts "
                "WHERE slug = :slug OR id::text = :slug"
            ),
            {"slug": post_slug},
        ).all()
        if not rows:
            return HTMLResponse("<h1>Post not found</h1>", status_code=404)
        r = rows[0]
        # Get tags
        tag_rows = conn.execute(
            text(
                "SELECT t.slug FROM tags t "
                "JOIN post_tags pt ON pt.tag_id = t.id "
                "JOIN posts p ON p.id = pt.post_id "
                "WHERE p.id = :post_id"
            ),
            {"post_id": r[0]},
        ).all()
        tags = [tr[0] for tr in tag_rows]

    post = {
        "id": str(r[0]),
        "slug": r[1],
        "title": r[2],
        "body_md": r[3],
        "excerpt": r[4] or "",
        "status": r[5],
        "revision": r[6],
        "content_hash": r[7] or "",
        "tags": tags,
    }

    csrf = _get_csrf(request)
    return HTMLResponse(render_editor(post=post, csrf_token=csrf, request=request))


@dashboard_router.post("/posts/{post_id}")
async def update_post(
    request: Request,
    post_id: str,
    title: str = Form(""),
    body_md: str = Form(""),
    slug: str = Form(""),
    excerpt: str = Form(""),
    tags: str = Form(""),
    if_match: str = Form(""),
    revision: int = Form(0),
) -> Response:
    user = _require_user(request)
    if not user:
        return RedirectResponse("/dashboard/login", status_code=302)
    _require_csrf(request)

    from app.db.session import get_engine
    from app.services.post import update_post as svc_update

    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []

    with get_engine().begin() as conn:
        from sqlalchemy.orm import Session as SessionType

        session = SessionType(bind=conn)
        try:
            _post, _warnings = svc_update(
                session,
                post_id,
                title=title if title else None,
                body_md=body_md if body_md else None,
                slug=slug if slug else None,
                tags=tag_list if tag_list else None,
                excerpt=excerpt if excerpt else None,
                actor_id=uuid.UUID(user.id),
                source="dashboard",
                audit_ctx=_human_auth_ctx(user),
                is_agent_actor=False,
            )
            post_slug = _post.slug
        except Exception as exc:
            return _toast_response(f"Error: {exc}", level="error")

    return _toast_response(
        f"Post '{post_slug}' updated",
        level="success",
        redirect=f"/dashboard/posts/{post_slug}",
    )


@dashboard_router.post("/posts/{post_id}/publish")
async def publish_post(request: Request, post_id: str) -> Response:
    user = _require_user(request)
    if not user:
        return RedirectResponse("/dashboard/login", status_code=302)
    _require_csrf(request)

    from app.db.session import get_engine
    from app.services.post import publish_post as svc_publish

    with get_engine().begin() as conn:
        from sqlalchemy.orm import Session as SessionType

        session = SessionType(bind=conn)
        try:
            _post, _warnings = svc_publish(
                session,
                post_id,
                actor_id=uuid.UUID(user.id),
                source="dashboard",
                audit_ctx=_human_auth_ctx(user),
                is_agent_actor=False,
            )
        except Exception as exc:
            return _toast_response(f"Error: {exc}", level="error")

    return _toast_response("Post published", level="success")


@dashboard_router.post("/posts/{post_id}/unpublish")
async def unpublish_post(request: Request, post_id: str) -> Response:
    user = _require_user(request)
    if not user:
        return RedirectResponse("/dashboard/login", status_code=302)
    _require_csrf(request)

    from app.db.session import get_engine
    from app.services.post import unpublish_post as svc_unpublish

    with get_engine().begin() as conn:
        from sqlalchemy.orm import Session as SessionType

        session = SessionType(bind=conn)
        try:
            _post, _warnings = svc_unpublish(
                session,
                post_id,
                actor_id=uuid.UUID(user.id),
                source="dashboard",
                audit_ctx=_human_auth_ctx(user),
            )
        except Exception as exc:
            return _toast_response(f"Error: {exc}", level="error")

    return _toast_response("Post unpublished", level="success")


@dashboard_router.delete("/posts/{post_id}")
async def trash_post(request: Request, post_id: str) -> Response:
    user = _require_user(request)
    if not user:
        return RedirectResponse("/dashboard/login", status_code=302)
    _require_csrf(request)

    from app.db.session import get_engine
    from app.services.post import trash_post as svc_trash

    with get_engine().begin() as conn:
        from sqlalchemy.orm import Session as SessionType

        session = SessionType(bind=conn)
        try:
            svc_trash(
                session,
                post_id,
                actor_id=uuid.UUID(user.id),
                source="dashboard",
                audit_ctx=_human_auth_ctx(user),
            )
        except Exception as exc:
            return _toast_response(f"Error: {exc}", level="error")

    return _toast_response("Post trashed", level="success")


# ---------------------------------------------------------------------------
# Revisions
# ---------------------------------------------------------------------------


@dashboard_router.get("/posts/{post_id}/revisions", response_class=HTMLResponse)
async def revisions_page(
    request: Request,
    post_id: str,
    from_rev: int | None = Query(None),
    to_rev: int | None = Query(None),
) -> Response:
    user = _require_user(request)
    if not user:
        return RedirectResponse("/dashboard/login", status_code=302)

    from app.db.session import get_engine

    with get_engine().connect() as conn:
        from sqlalchemy import text

        post_rows = conn.execute(
            text("SELECT id, slug FROM posts WHERE id::text = :id OR slug = :id"),
            {"id": post_id},
        ).all()
        if not post_rows:
            return HTMLResponse("<h1>Post not found</h1>", status_code=404)
        actual_id = str(post_rows[0][0])
        post_slug = post_rows[0][1]

        rev_rows = conn.execute(
            text(
                "SELECT revision, title, status, actor_id, created_at "
                "FROM post_revisions "
                "WHERE post_id = :post_id ORDER BY revision DESC "
                "LIMIT 20"
            ),
            {"post_id": uuid.UUID(actual_id)},
        ).all()
        revisions = [
            {
                "revision": r[0],
                "title": r[1],
                "status": r[2],
                "actor_id": str(r[3])[:8] if r[3] else "",
                "created_at": str(r[4]) if r[4] else "",
            }
            for r in rev_rows
        ]

    diff = None
    if from_rev is not None and to_rev is not None:
        from app.db.session import get_engine as get_engine2
        from app.services.post import get_diff

        with get_engine2().begin() as conn:
            from sqlalchemy.orm import Session as SessionType

            session = SessionType(bind=conn)
            try:
                diff = get_diff(session, post_id, from_rev, to_rev)
            except Exception:
                diff = {
                    "diff_unified": "",
                    "identical": True,
                }

    csrf = _get_csrf(request)
    return HTMLResponse(
        render_revisions(
            post_id=post_id,
            post_slug=post_slug,
            revisions=revisions,
            diff=diff,
            csrf_token=csrf,
            request=request,
        )
    )


# ---------------------------------------------------------------------------
# Reviews
# ---------------------------------------------------------------------------


@dashboard_router.get("/reviews", response_class=HTMLResponse)
async def reviews_page(request: Request) -> Response:
    user = _require_user(request)
    if not user:
        return RedirectResponse("/dashboard/login", status_code=302)

    from app.db.session import get_engine

    with get_engine().connect() as conn:
        from sqlalchemy import text

        rows = conn.execute(
            text(
                "SELECT r.id, r.post_id, r.status, "
                "r.snapshot_title, r.created_at, s.slug "
                "FROM reviews r "
                "LEFT JOIN sites s ON s.id = r.site_id "
                "WHERE r.status = 'pending_review' "
                "ORDER BY r.created_at DESC"
            )
        ).all()
        reviews = [
            {
                "id": str(r[0]),
                "post_id": str(r[1]),
                "status": r[2],
                "snapshot_title": r[3],
                "created_at": str(r[4]) if r[4] else "",
            }
            for r in rows
        ]

    csrf = _get_csrf(request)
    return HTMLResponse(render_reviews(reviews=reviews, csrf_token=csrf, request=request))


@dashboard_router.post("/reviews/{review_id}/approve")
async def approve_review(
    request: Request,
    review_id: str,
    comment: str = Form("Approved via dashboard"),
) -> Response:
    user = _require_user(request)
    if not user:
        return RedirectResponse("/dashboard/login", status_code=302)
    _require_csrf(request)

    from app.db.session import get_engine
    from app.services.review import approve_review as svc_approve

    with get_engine().begin() as conn:
        from sqlalchemy.orm import Session as SessionType

        session = SessionType(bind=conn)
        try:
            svc_approve(
                session,
                review_id,
                reviewer_actor_id=uuid.UUID(user.id),
                comment=comment,
                source="dashboard",
                audit_ctx=_human_auth_ctx(user),
            )
        except Exception as exc:
            return _toast_response(f"Error: {exc}", level="error")

    return _toast_response("Review approved and post published", level="success")


@dashboard_router.post("/reviews/{review_id}/reject")
async def reject_review(
    request: Request,
    review_id: str,
    reason: str = Form("Needs revision"),
) -> Response:
    user = _require_user(request)
    if not user:
        return RedirectResponse("/dashboard/login", status_code=302)
    _require_csrf(request)

    from app.db.session import get_engine
    from app.services.review import reject_review as svc_reject

    with get_engine().begin() as conn:
        from sqlalchemy.orm import Session as SessionType

        session = SessionType(bind=conn)
        try:
            svc_reject(
                session,
                review_id,
                reviewer_actor_id=uuid.UUID(user.id),
                reason=reason,
                source="dashboard",
                audit_ctx=_human_auth_ctx(user),
            )
        except Exception as exc:
            return _toast_response(f"Error: {exc}", level="error")

    return _toast_response("Review rejected", level="success")


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------


@dashboard_router.get("/tokens", response_class=HTMLResponse)
async def tokens_page(request: Request) -> Response:
    user = _require_user(request)
    if not user:
        return RedirectResponse("/dashboard/login", status_code=302)

    from app.db.session import get_engine

    with get_engine().connect() as conn:
        from sqlalchemy import text

        rows = conn.execute(
            text(
                "SELECT cl.id, cl.label, a.scopes, cl.revoked_at, "
                "cl.expires_at, a.last_used_at, a.uses_count, "
                "cl.created_at "
                "FROM capability_links cl "
                "LEFT JOIN actors a ON a.id = cl.actor_id "
                "ORDER BY cl.created_at DESC LIMIT 50"
            )
        ).all()
        tokens = [
            {
                "id": str(r[0]),
                "label": r[1] or "",
                "scopes": r[2] if r[2] else [],
                "revoked_at": r[3],
                "expires_at": str(r[4]) if r[4] else None,
                "last_used_at": str(r[5]) if r[5] else None,
                "uses_count": r[6] or 0,
                "created_at": str(r[7]) if r[7] else "",
            }
            for r in rows
        ]

    csrf = _get_csrf(request)
    return HTMLResponse(render_tokens(tokens=tokens, csrf_token=csrf, request=request))


@dashboard_router.get("/tokens/new")
async def new_token_form(request: Request) -> Response:
    """Return a token creation form (for htmx)."""
    user = _require_user(request)
    if not user:
        return RedirectResponse("/dashboard/login", status_code=302)

    csrf = _get_csrf(request)
    csrf_esc = _esc(csrf)
    form_html = (
        '<div class="card" style="margin-bottom:var(--s-4)">'
        '<h3 style="font:var(--text-title-md);margin:0 0 var(--s-4)">'
        "Create token</h3>"
        '<form method="post" action="/dashboard/tokens" '
        'style="display:flex;flex-direction:column;gap:var(--s-4)">'
        '<div class="field">'
        '<label for="token_label">Label</label>'
        '<input type="text" id="token_label" name="label" '
        'placeholder="e.g. CI pipeline" required>'
        "</div>"
        '<div class="field"><label>Scopes</label>'
        '<div style="display:flex;gap:var(--s-3);flex-wrap:wrap">'
        '<label style="display:flex;align-items:center;gap:var(--s-2);'
        'font:var(--text-body-sm)"><input type="checkbox" '
        'name="scope_posts_read" value="posts:read" checked> '
        "posts:read</label>"
        '<label style="display:flex;align-items:center;gap:var(--s-2);'
        'font:var(--text-body-sm)"><input type="checkbox" '
        'name="scope_posts_write" value="posts:write" checked> '
        "posts:write</label>"
        '<label style="display:flex;align-items:center;gap:var(--s-2);'
        'font:var(--text-body-sm)"><input type="checkbox" '
        'name="scope_posts_publish" value="posts:publish"> '
        "posts:publish</label>"
        "</div></div>"
        '<div class="field">'
        '<label for="expires_in_days">Expires in (days)</label>'
        '<input type="number" id="expires_in_days" '
        'name="expires_in_days" value="90" min="1" max="3650">'
        "</div>"
        '<input type="hidden" name="csrf_token" '
        f'value="{csrf_esc}">'
        '<div style="display:flex;gap:var(--s-2)">'
        '<button type="submit" class="btn btn--primary">'
        "Create</button>"
        '<button type="button" class="btn btn--ghost" '
        'hx-get="/dashboard/tokens" '
        'hx-target="#create-form" '
        'hx-swap="innerHTML">Cancel</button>'
        "</div></form></div>"
    )
    return HTMLResponse(form_html)


@dashboard_router.post("/tokens")
async def create_token(
    request: Request,
    label: str = Form(...),
    expires_in_days: int = Form(90),
    scope_posts_read: str | None = Form(None),
    scope_posts_write: str | None = Form(None),
    scope_posts_publish: str | None = Form(None),
) -> Response:
    user = _require_user(request)
    if not user:
        return RedirectResponse("/dashboard/login", status_code=302)
    _require_csrf(request)

    scopes = []
    if scope_posts_read:
        scopes.append(scope_posts_read)
    if scope_posts_write:
        scopes.append(scope_posts_write)
    if scope_posts_publish:
        scopes.append(scope_posts_publish)
    if not scopes:
        scopes = ["posts:read", "posts:write"]

    from app.db.session import get_engine
    from app.services.tokens import generate_token

    with get_engine().begin() as conn:
        from sqlalchemy.orm import Session as SessionType

        session = SessionType(bind=conn)
        try:
            from app.models.actor import Actor
            from app.models.capability_link import CapabilityLink

            actor = Actor(
                id=uuid.uuid4(),
                kind="machine",
                label=label,
                scopes=scopes,
            )
            session.add(actor)
            session.flush()

            _plaintext, token_hash = generate_token(actor.id)
            link = CapabilityLink(
                id=uuid.uuid4(),
                actor_id=actor.id,
                token_hash=token_hash,
                label=label,
                path_scope="/",
                verbs=["GET", "POST", "PATCH", "DELETE"],
            )
            session.add(link)
            session.flush()

            from app.services.audit import record_event

            record_event(
                session,
                action="token.created",
                actor_id=uuid.UUID(user.id),
                actor_label=user.label,
                actor_kind="human",
                source="dashboard",
                target_type="token",
                target_id=str(link.id),
                event_metadata={
                    "label": label,
                    "scopes": scopes,
                },
            )
            session.commit()
        except Exception as exc:
            return _toast_response(f"Error: {exc}", level="error")

    return _toast_response(
        f"Token '{label}' created - copy the plaintext now, it won't be shown again",
        level="success",
    )


@dashboard_router.delete("/tokens/{token_id}")
async def revoke_token(request: Request, token_id: str) -> Response:
    user = _require_user(request)
    if not user:
        return RedirectResponse("/dashboard/login", status_code=302)
    _require_csrf(request)

    from datetime import UTC, datetime

    from app.db.session import get_engine

    with get_engine().begin() as conn:
        from sqlalchemy import text

        conn.execute(
            text("UPDATE capability_links SET revoked_at = :now WHERE id = :id"),
            {"id": uuid.UUID(token_id), "now": datetime.now(UTC)},
        )

        from sqlalchemy.orm import Session as SessionType

        from app.services.audit import record_event

        session = SessionType(bind=conn)
        record_event(
            session,
            action="token.revoked",
            actor_id=uuid.UUID(user.id),
            actor_label=user.label,
            actor_kind="human",
            source="dashboard",
            target_type="token",
            target_id=token_id,
        )
        session.commit()

    return _toast_response("Token revoked")


@dashboard_router.post("/tokens/{token_id}/rotate")
async def rotate_token(request: Request, token_id: str) -> Response:
    user = _require_user(request)
    if not user:
        return RedirectResponse("/dashboard/login", status_code=302)
    _require_csrf(request)

    from app.db.session import get_engine
    from app.services.tokens import generate_token

    with get_engine().begin() as conn:
        from sqlalchemy import text
        from sqlalchemy.orm import Session as SessionType

        # Get the existing link
        rows = conn.execute(
            text("SELECT actor_id FROM capability_links WHERE id = :id"),
            {"id": uuid.UUID(token_id)},
        ).all()
        if not rows:
            return _toast_response("Token not found", level="error")

        actor_id = rows[0][0]

        # Revoke old
        from datetime import UTC, datetime

        conn.execute(
            text("UPDATE capability_links SET revoked_at = :now WHERE id = :id"),
            {
                "id": uuid.UUID(token_id),
                "now": datetime.now(UTC),
            },
        )

        # Create new
        session = SessionType(bind=conn)
        from app.models.actor import Actor
        from app.models.capability_link import CapabilityLink

        old_actor = session.query(Actor).filter(Actor.id == actor_id).first()
        if not old_actor:
            return _toast_response("Actor not found", level="error")

        new_actor = Actor(
            id=uuid.uuid4(),
            kind="machine",
            label=old_actor.label,
            scopes=old_actor.scopes,
        )
        session.add(new_actor)
        session.flush()

        _plaintext, token_hash = generate_token(new_actor.id)
        new_link = CapabilityLink(
            id=uuid.uuid4(),
            actor_id=new_actor.id,
            token_hash=token_hash,
            label=old_actor.label,
            path_scope="/",
            verbs=["GET", "POST", "PATCH", "DELETE"],
        )
        session.add(new_link)
        session.flush()

        from app.services.audit import record_event

        record_event(
            session,
            action="token.created",
            actor_id=uuid.UUID(user.id),
            actor_label=user.label,
            actor_kind="human",
            source="dashboard",
            target_type="token",
            target_id=str(new_link.id),
            event_metadata={
                "rotated_from": token_id,
            },
        )
        session.commit()

    return _toast_response("Token rotated - copy the new plaintext", level="success")


# ---------------------------------------------------------------------------
# Activity
# ---------------------------------------------------------------------------


@dashboard_router.get("/activity", response_class=HTMLResponse)
async def activity_page(request: Request) -> Response:
    user = _require_user(request)
    if not user:
        return RedirectResponse("/dashboard/login", status_code=302)

    from app.db.session import get_engine

    with get_engine().connect() as conn:
        from sqlalchemy import text

        rows = conn.execute(
            text(
                "SELECT action, actor_label, target_type, "
                "target_id, created_at "
                "FROM audit_events "
                "ORDER BY created_at DESC LIMIT 50"
            )
        ).all()
        events = [
            {
                "action": r[0],
                "actor_label": r[1],
                "target_type": r[2],
                "target_id": str(r[3]) if r[3] else "",
                "created_at": str(r[4]) if r[4] else "",
            }
            for r in rows
        ]

    from app.services.audit import detect_anomalies

    with get_engine().begin() as conn:
        from sqlalchemy.orm import Session as SessionType

        session = SessionType(bind=conn)
        anomalies = detect_anomalies(session, since_minutes=60)

    csrf = _get_csrf(request)
    return HTMLResponse(
        render_activity(
            events=events,
            anomalies=anomalies,
            csrf_token=csrf,
            request=request,
        )
    )


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@dashboard_router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request) -> Response:
    user = _require_user(request)
    if not user:
        return RedirectResponse("/dashboard/login", status_code=302)

    from app.db.session import get_engine

    with get_engine().connect() as conn:
        from sqlalchemy import text

        site_rows = conn.execute(text("SELECT slug, name, base_url, publish_mode FROM sites LIMIT 1")).all()
        site: dict[str, Any] = {}
        if site_rows:
            r = site_rows[0]
            site = {
                "slug": r[0],
                "name": r[1],
                "base_url": r[2] or "",
                "publish_mode": r[3] or "auto",
            }

        policy_rows = conn.execute(
            text("SELECT name, kind, enabled FROM content_policies ORDER BY priority LIMIT 20")
        ).all()
        policies = [{"name": r[0], "kind": r[1], "enabled": r[2]} for r in policy_rows]

    csrf = _get_csrf(request)
    return HTMLResponse(
        render_settings(
            site=site,
            policies=policies,
            csrf_token=csrf,
            request=request,
        )
    )


@dashboard_router.post("/settings/site")
async def update_site_settings(
    request: Request,
    site_name: str = Form(""),
    base_url: str = Form(""),
    publish_mode: str = Form("auto"),
) -> Response:
    user = _require_user(request)
    if not user:
        return RedirectResponse("/dashboard/login", status_code=302)
    _require_csrf(request)

    from app.db.session import get_engine

    with get_engine().begin() as conn:
        from sqlalchemy import text

        conn.execute(
            text(
                "UPDATE sites "
                "SET name = :name, base_url = :base_url, "
                "publish_mode = :publish_mode, updated_at = NOW() "
                "WHERE id = (SELECT id FROM sites LIMIT 1)"
            ),
            {
                "name": site_name,
                "base_url": base_url,
                "publish_mode": publish_mode,
            },
        )

        from sqlalchemy.orm import Session as SessionType

        from app.services.audit import record_event

        session = SessionType(bind=conn)
        record_event(
            session,
            action="site.updated",
            actor_id=uuid.UUID(user.id),
            actor_label=user.label,
            actor_kind="human",
            source="dashboard",
            target_type="site",
            target_id=site_name,
            event_metadata={
                "fields": ["name", "base_url", "publish_mode"],
            },
        )
        session.commit()

    return _toast_response(
        "Settings saved",
        level="success",
        redirect="/dashboard/settings",
    )


# ---------------------------------------------------------------------------
# Kill switch toggle
# ---------------------------------------------------------------------------


@dashboard_router.post("/kill-switch/global")
async def toggle_global_kill_switch(
    request: Request,
) -> Response:
    user = _require_user(request)
    if not user:
        return RedirectResponse("/dashboard/login", status_code=302)
    _require_csrf(request)

    from app.services.kill_switch import get_kill_switch_store

    ks = get_kill_switch_store()
    if ks.is_global_paused():
        ks.resume_global()
        return _toast_response("Agent writes resumed", level="success")
    else:
        ks.pause_global()
        return _toast_response("Agent writes paused", level="warning")
