"""Media serving routes (ticket #20).

Serves immutable, content-hashed media assets:
    GET /media/{sha256}/{filename}

In production, this would be handled by a CDN/static server.
For development, we serve from the database metadata.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.domain.errors import NotFoundError
from app.models.asset import Asset

router = APIRouter(tags=["media"], include_in_schema=False)


@router.get(
    "/media/{sha256}/{filename}",
    summary="Serve an immutable media asset",
)
def serve_media(
    sha256: str,
    filename: str,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Serve a media asset with immutable cache headers."""
    asset = db.query(Asset).filter(Asset.sha256 == sha256, Asset.deleted_at.is_(None)).first()
    if asset is None:
        raise NotFoundError(f"Media asset '{sha256}/{filename}' not found.")

    return JSONResponse(
        content={"message": "Asset stored — in production, this serves the file from S3/CDN."},
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "Content-Type": asset.content_type or "application/octet-stream",
        },
    )
