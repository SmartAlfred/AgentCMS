"""Static export API (#23).

``GET /v1/sites/{site}/export?format=tar.gz|zip&since=<iso>`` returns a
deterministic archive of the site's static export.  Without ``since`` it is the
full bundle (posts, markdown, JSON, feeds, sitemap, manifest).  With ``since``
it is a delta bundle containing only the post files whose ``updated_at`` falls
after the timestamp, so a pull-based static host can merge them.
"""

from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.orm import Session

from app.auth import AuthContext, require_auth
from app.db.session import get_db
from app.domain.errors import InvalidQueryError
from app.models.site import Site
from app.services.export import (
    export_site,
    render_archive,
)

router = APIRouter()

_ARCHIVE_MEDIA_TYPES = {
    "tar.gz": "application/gzip",
    "zip": "application/zip",
}


def _parse_since(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        since = datetime.fromisoformat(value)
    except ValueError as exc:
        raise InvalidQueryError(
            f"'since' must be an ISO-8601 timestamp, got '{value}'.",
            hint="Pass e.g. since=2026-09-18T12:00:00Z or since=2026-09-18.",
        ) from exc
    if since.tzinfo is None:
        from datetime import UTC

        since = since.replace(tzinfo=UTC)
    return since


@router.get(
    "/sites/{site_slug}/export",
    summary="Export a site as a deterministic static archive",
    tags=["sites"],
    response_description="The static export archive (tar.gz or zip).",
)
def export_archive(
    request: Request,
    site_slug: str,
    format: Annotated[str, Query(pattern="^(tar\\.gz|zip)$")] = "tar.gz",
    since: str | None = Query(None, description="Only include posts updated after this ISO timestamp"),
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(require_auth),
) -> Response:
    """Stream the site's static export, optionally as a post-update delta."""
    site = db.query(Site).filter(Site.slug == site_slug).first()
    if site is None:
        from app.services.export import ExportSiteNotFoundError

        raise ExportSiteNotFoundError(site_slug)

    since_dt = _parse_since(since)
    base_url: str | None = None
    if not site.base_url:
        base_url = f"{request.url.scheme}://{request.url.netloc}"

    delta_only = since_dt is not None
    with tempfile.TemporaryDirectory(prefix="agentcms-export-") as tmp_dir:
        result = export_site(
            db,
            site_slug,
            Path(tmp_dir),
            since=since_dt,
            base_url=base_url,
            delta_only=delta_only,
        )
        files: dict[str, bytes] = {}
        for rel in sorted(result.manifest["files"]):
            src = Path(tmp_dir) / rel
            if src.is_file():
                files[rel] = src.read_bytes()
        manifest_path = Path(tmp_dir) / "manifest.json"
        if manifest_path.is_file():
            files["manifest.json"] = manifest_path.read_bytes()
        archive = render_archive(files, format)

    media_type = _ARCHIVE_MEDIA_TYPES[format]
    filename = f"{site_slug}-export.{format}"
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Type": media_type,
        "Cache-Control": "no-store",
    }
    return Response(content=archive, headers=headers)
