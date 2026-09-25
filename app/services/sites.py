"""Site lifecycle service (#45).

A fresh AgentCMS had no way to create the one thing everything else hangs off:
a site.  ``POST /v1/sites`` simply did not exist, the dashboard can only *update*
an existing row, and the documented first run ("create a site") 404'd for every
self-hoster — which is what turned the self-host E2E job (#37) red.  These three
operations (create / list / get) are what the API, the scripts and the dashboard
all need; the dashboard form that consumes them lands with the rest of #45.
"""

from __future__ import annotations

import re
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.errors import ConflictError, DomainError, SiteNotFoundError
from app.models.site import Site
from app.services.audit import record_event

# Site slugs use the same shape as post slugs: lowercase, hyphen-separated.
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

PUBLISH_MODES: tuple[str, ...] = ("auto", "require_review")


class InvalidSiteError(DomainError):
    """A site payload that cannot be stored (bad slug, name or publish mode)."""

    status_code = 422
    code = "invalid-site"
    title = "Invalid site"

    def __init__(self, message: str, *, field: str, hint: str | None = None) -> None:
        super().__init__(message, hint=hint, extra={"field": field})


class SiteSlugConflictError(ConflictError):
    """A site with that slug already exists."""

    code = "site-slug-conflict"
    title = "Site slug already in use"

    def __init__(self, slug: str) -> None:
        super().__init__(
            f"A site with slug '{slug}' already exists.",
            hint="GET /v1/sites/{slug} to read it, or choose a different slug.",
            extra={"slug": slug},
        )


def get_site(session: Session, slug: str) -> Site:
    """Return the site with ``slug`` or raise :class:`SiteNotFoundError`."""
    site = session.execute(select(Site).where(Site.slug == slug)).scalar_one_or_none()
    if site is None:
        raise SiteNotFoundError(slug)
    return site


def list_sites(session: Session) -> list[Site]:
    """Return every site, oldest first (deterministic order for scripts)."""
    return list(session.execute(select(Site).order_by(Site.created_at, Site.slug)).scalars().all())


def create_site(
    session: Session,
    *,
    slug: str,
    name: str,
    base_url: str | None = None,
    publish_mode: str = "auto",
    actor_id: uuid.UUID | None = None,
    actor_label: str | None = None,
    actor_kind: str = "machine",
    request_id: str | None = None,
) -> Site:
    """Create a site, audit it, and return it.

    Raises :class:`InvalidSiteError` for a bad slug/name/publish mode and
    :class:`SiteSlugConflictError` when the slug is taken — ``scripts/deploy_smoke.sh``
    and ``scripts/upgrade_smoke.sh`` both fall back to ``GET /v1/sites/{slug}`` on
    a re-run, so a duplicate must be a clean 409 and never an overwrite.
    """
    slug = (slug or "").strip().lower()
    if not _SLUG_RE.match(slug) or len(slug) > 128:
        raise InvalidSiteError(
            f"'{slug}' is not a valid site slug.",
            field="slug",
            hint="Slugs are lowercase, hyphen-separated: letters, digits and '-' (e.g. 'my-blog').",
        )
    name = (name or "").strip()
    if not name:
        raise InvalidSiteError("A site needs a non-empty name.", field="name")
    if len(name) > 256:
        raise InvalidSiteError("A site name may be at most 256 characters.", field="name")
    if publish_mode not in PUBLISH_MODES:
        raise InvalidSiteError(
            f"publish_mode must be one of: {', '.join(PUBLISH_MODES)}.",
            field="publish_mode",
        )
    if session.execute(select(Site.id).where(Site.slug == slug)).first() is not None:
        raise SiteSlugConflictError(slug)

    site = Site(
        id=uuid.uuid4(),
        slug=slug,
        name=name,
        base_url=(base_url or None),
        publish_mode=publish_mode,
    )
    session.add(site)
    session.flush()
    record_event(
        session,
        action="site.created",
        actor_id=actor_id,
        actor_label=actor_label,
        actor_kind=actor_kind,
        target_type="site",
        target_id=str(site.id),
        request_id=request_id,
        event_metadata={"slug": site.slug, "publish_mode": site.publish_mode},
    )
    session.commit()
    session.refresh(site)
    return site
