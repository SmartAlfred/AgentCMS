"""MCP resources for AgentCMS.

Resources:
- agentcms://llms.txt — full instruction sheet for models
- agentcms://site/{site}/posts — list of posts for a site
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable

from mcp.types import ReadResourceResult, Resource, TextResourceContents

from app.db.session import session_scope
from app.docs_content import LLMS_TXT
from app.models.site import Site
from app.services.post import _post_to_dict
from app.services.post import list_posts as svc_list_posts

RESOURCES = [
    Resource(
        uri="agentcms://llms.txt",
        name="AgentCMS Instructions",
        description="Full instruction sheet for AI agents using AgentCMS",
        mime_type="text/plain",
    ),
    Resource(
        uri="agentcms://site/{site}/posts",
        name="Site Posts",
        description="List of posts for a site (templated by site slug)",
        mime_type="application/json",
    ),
]


async def handle_llms_txt_resource() -> ReadResourceResult:
    """Return the full llms.txt instruction sheet for models."""
    return ReadResourceResult(
        contents=[
            TextResourceContents(
                uri="agentcms://llms.txt",
                mime_type="text/plain",
                text=LLMS_TXT,
            )
        ]
    )


async def handle_site_posts_resource(site: str) -> ReadResourceResult:
    """Return a list of posts for a site as JSON."""
    try:
        with session_scope() as db:
            site_obj = db.query(Site).filter(Site.slug == site).first()
            if site_obj is None:
                return ReadResourceResult(
                    contents=[
                        TextResourceContents(
                            uri=f"agentcms://site/{site}/posts",
                            mime_type="application/json",
                            text=json.dumps({"error": f"Site '{site}' not found"}),
                        )
                    ]
                )

            items, _ = svc_list_posts(db, site, limit=100)
            posts = [_post_to_dict(p, site, session=db) for p in items]

            return ReadResourceResult(
                contents=[
                    TextResourceContents(
                        uri=f"agentcms://site/{site}/posts",
                        mime_type="application/json",
                        text=json.dumps(
                            {"site": site, "count": len(posts), "posts": posts},
                            indent=2,
                            default=str,
                        ),
                    )
                ]
            )
    except Exception as e:
        return ReadResourceResult(
            contents=[
                TextResourceContents(
                    uri=f"agentcms://site/{site}/posts",
                    mime_type="application/json",
                    text=json.dumps({"error": str(e)}),
                )
            ]
        )


RESOURCE_HANDLERS: dict[str, Callable[..., Awaitable[ReadResourceResult]]] = {
    "agentcms://llms.txt": handle_llms_txt_resource,
    "agentcms://site/{site}/posts": handle_site_posts_resource,
}


def get_all_resources() -> list[Resource]:
    """Return all registered resources."""
    return RESOURCES


async def handle_resource_read(uri: str) -> ReadResourceResult:
    """Read a resource by URI."""
    # Handle templated resources
    if uri.startswith("agentcms://site/") and uri.endswith("/posts"):
        # Extract site slug from URI
        # agentcms://site/{site}/posts
        prefix = "agentcms://site/"
        suffix = "/posts"
        site = uri[len(prefix) : -len(suffix)]
        templated = RESOURCE_HANDLERS["agentcms://site/{site}/posts"]
        return await templated(site)

    # Exact match resources
    exact = RESOURCE_HANDLERS.get(uri)
    if exact is not None:
        return await exact()

    raise ValueError(f"Unknown resource: {uri}")
