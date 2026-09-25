"""Site endpoints (#45).

``POST /v1/sites``   — create a site (requires the ``sites:write`` scope)
``GET  /v1/sites``   — list sites (``sites:read``)
``GET  /v1/sites/{slug}`` — read one site (``sites:read``)

A self-hoster's very first request is "create my site"; before #45 the only way
was ``scripts/seed.py`` or SQL, so the quickstart, ``make selfhost-verify`` and
the #37 self-host E2E job all failed with a 404 against a route that never
existed.  These endpoints are the API-first bootstrap the docs already claimed.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.auth import AuthContext, require_auth
from app.db.session import get_db
from app.models.site import Site
from app.services.sites import create_site, get_site, list_sites

from .schemas import SiteCreateRequest, SiteListResponse, SiteRead

router = APIRouter()

DbSession = Annotated[Session, Depends(get_db)]


def _read(site: Site) -> SiteRead:
    return SiteRead(
        id=str(site.id),
        slug=site.slug,
        name=site.name,
        base_url=site.base_url,
        publish_mode=site.publish_mode,
        created_at=site.created_at,
    )


@router.post(
    "/sites",
    summary="Create a site",
    status_code=201,
    response_model=SiteRead,
    tags=["sites"],
)
def create_site_endpoint(
    body: SiteCreateRequest,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_auth),
) -> SiteRead:
    """Create a site and return it (id + slug are what every other endpoint needs)."""
    site = create_site(
        db,
        slug=body.slug,
        name=body.name,
        base_url=body.base_url,
        publish_mode=body.publish_mode,
        actor_id=auth.actor_id,
        actor_label=auth.label,
        request_id=getattr(request.state, "request_id", None),
    )
    return _read(site)


@router.get(
    "/sites",
    summary="List sites",
    response_model=SiteListResponse,
    tags=["sites"],
)
def list_sites_endpoint(
    db: DbSession,
    auth: AuthContext = Depends(require_auth),
) -> SiteListResponse:
    """List every site on this instance."""
    sites = [_read(site) for site in list_sites(db)]
    return SiteListResponse(items=sites, count=len(sites))


@router.get(
    "/sites/{site_slug}",
    summary="Read one site",
    response_model=SiteRead,
    tags=["sites"],
)
def get_site_endpoint(
    site_slug: str,
    db: DbSession,
    auth: AuthContext = Depends(require_auth),
) -> SiteRead:
    """Read a single site by slug (404 when it does not exist)."""
    return _read(get_site(db, site_slug))
