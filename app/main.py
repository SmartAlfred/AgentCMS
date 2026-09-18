"""AgentCMS API application factory (#2).

Contract decisions this module is responsible for keeping intact:

* ``GET /openapi.json`` is generated from the code (FastAPI emits OpenAPI 3.1)
  and is the single source the agent-facing surfaces are derived from (#9).
* every error is ``application/problem+json`` (#10).
* ``/healthz`` and ``/readyz`` are unauthenticated.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi

from app.api.docs import router as docs_router
from app.api.health import router as health_router
from app.api.llms import router as llms_router
from app.config import Settings, get_settings
from app.db.session import dispose_engine
from app.errors import register_error_handlers
from app.logging import configure_logging

DESCRIPTION = """
AgentCMS is a CMS whose first user is an AI agent.

* **Create never publishes.** `POST …/posts` always returns a draft; publishing is
  a separate, separately-audited call.
* **Errors instruct.** Every failure is `application/problem+json` with a
  `hint` that says how to fix the request.
* **The contract is generated.** This document, `GET /llms.txt` and the
  capability-link landing pages all come from the same route definitions.
""".strip()

TAGS_METADATA = [
    {"name": "ops", "description": "Liveness and readiness. Unauthenticated."},
    {"name": "posts", "description": "Create, read, update, publish, unpublish and trash posts."},
    {"name": "search", "description": "Full-text search with faceted filters."},
    {"name": "tags", "description": "Tag listing with counts and merge."},
    {"name": "sites", "description": "Content containers (site slug, publish mode)."},
    {"name": "assets", "description": "Media uploads: presigned URLs, inline upload, variants."},
    {"name": "webhooks", "description": "Webhook subscriptions: CRUD, test ping, redeliver."},
    {"name": "events", "description": "Pollable event feed for agents that cannot receive webhooks."},
]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # The MCP transport at /mcp is a mounted sub-application, and Starlette does
    # not run a mounted app's lifespan — so the parent owns the session manager.
    transport = getattr(app.state, "mcp_transport", None)
    if transport is None:
        yield
    else:
        async with transport.lifespan():
            yield
    dispose_engine()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the ASGI application."""

    settings = settings or get_settings()
    configure_logging(settings)

    from app.observability import configure_observability

    configure_observability(settings)

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description=DESCRIPTION,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
        docs_url=None,  # replaced by Scalar at /docs
        redoc_url=None,  # replaced by ReDoc at /redoc
        openapi_tags=TAGS_METADATA,
        lifespan=lifespan,
    )
    app.state.settings = settings

    from app.middleware import BodySizeLimitMiddleware, RateLimitHeadersMiddleware, RequestContextMiddleware

    # Order matters: outermost middleware runs first on request, last on response.
    # 1. Body size limit — reject cheaply before buffering
    app.add_middleware(BodySizeLimitMiddleware)
    # 2. Rate limit headers — attach X-RateLimit-* to every response
    app.add_middleware(RateLimitHeadersMiddleware)
    # 3. Request context — request id, logging
    app.add_middleware(RequestContextMiddleware)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    register_error_handlers(app)

    app.include_router(health_router)
    from app.api.observability import router as observability_router

    app.include_router(observability_router)
    if settings.docs_enabled:
        app.include_router(docs_router)
    app.include_router(llms_router)
    register_v1_routes(app)
    # Mounted before the public router: its catch-alls (/{site_slug}) would
    # otherwise swallow /mcp, and Starlette matches routes in registration order.
    register_mcp_routes(app)
    register_public_routes(app)
    register_dashboard_routes(app)

    # Customise the generated OpenAPI document: add security schemes and servers.
    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
            tags=app.openapi_tags,
        )
        schema["components"] = schema.get("components", {})
        schema["components"]["securitySchemes"] = {
            "bearerAuth": {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "API token (acms_...)",
                "description": "Standard API token. Get one via POST /v1/admin/tokens.",
            },
            "capabilityToken": {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "Capability token (cap_...)",
                "description": (
                    "Pre-scoped capability token from a /c/{token} link. "
                    "Visit the link to see its permissions."
                ),
            },
        }
        schema["servers"] = [{"url": "/", "description": "Current instance"}]
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi  # type: ignore[method-assign]

    return app


def register_v1_routes(app: FastAPI) -> None:
    """Mount the versioned content API (implemented in #4, #5, #6)."""

    from app.api.admin_rate_limits import router as admin_rl_router
    from app.api.media import router as media_router
    from app.api.v1.capability_links import router as cap_router
    from app.api.v1.routes import router as v1_router

    app.include_router(v1_router, prefix="/v1")
    app.include_router(media_router)
    app.include_router(admin_rl_router)
    app.include_router(cap_router)


def register_public_routes(app: FastAPI) -> None:
    """Mount the public read surface (ticket #8)."""
    from app.api.public import router as public_router

    # Public routes must be last — they use catch-all patterns like /{site}/{slug}
    app.include_router(public_router)


def register_dashboard_routes(app: FastAPI) -> None:
    """Mount the human dashboard (ticket #18)."""
    from app.dashboard import router as dashboard_router

    app.include_router(dashboard_router)


def register_mcp_routes(app: FastAPI) -> None:
    """Mount the MCP Streamable-HTTP endpoint at /mcp (ticket #22)."""
    from app.mcp.server import create_mcp_server
    from app.mcp.transport import create_streamable_http_transport

    transport = create_streamable_http_transport(create_mcp_server())
    # Starlette dispatches a *class instance* endpoint as a raw ASGI app (only
    # functions are wrapped as request handlers) - which is what the transport
    # needs. FastAPI's add_route signature is typed for request handlers, hence
    # the ignore. app.mount() cannot be used: it 307-redirects "/mcp" -> "/mcp/",
    # and MCP clients POST to the advertised URL exactly.
    app.add_route("/mcp", transport.app, methods=["GET", "POST", "DELETE"])  # type: ignore[arg-type]
    app.state.mcp_transport = transport


app = create_app()
