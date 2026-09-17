"""Admin token management API (#5).

Endpoints for creating, listing, revoking, and rotating API tokens.
These endpoints are dashboard-authenticated (humans only).

Token format: ``acms_<actor_id_hex>_<secret>``
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.domain.errors import NotFoundError
from app.models.actor import Actor
from app.models.capability_link import CapabilityLink
from app.services.tokens import (
    ALL_SCOPES,
    DEFAULT_EXPIRY_DAYS,
    DEFAULT_SCOPES,
    generate_token,
)

router = APIRouter(prefix="/admin/tokens", tags=["admin"])


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------


class TokenCreateRequest(BaseModel):
    """Request body for POST /v1/admin/tokens."""

    model_config = ConfigDict(str_strip_whitespace=True)

    label: str = Field(..., min_length=1, max_length=256, description="Human-readable label for the token")
    scopes: list[str] = Field(default_factory=lambda: list(DEFAULT_SCOPES))
    site_id: uuid.UUID | None = None
    expires_in_days: int | None = Field(default=DEFAULT_EXPIRY_DAYS, ge=1, le=3650)
    ip_allowlist: str | None = None


class TokenRead(BaseModel):
    """Token metadata returned by the API (never includes the plaintext)."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    actor_id: str
    label: str
    scopes: list[str]
    site_id: str | None = None
    expires_at: datetime | None = None
    last_used_at: datetime | None = None
    uses_count: int = 0
    revoked_at: datetime | None = None
    created_at: datetime
    ip_allowlist: str | None = None


class TokenCreateResponse(BaseModel):
    """Response for POST /v1/admin/tokens — includes plaintext ONCE."""

    token: str
    id: str
    actor_id: str
    label: str
    scopes: list[str]
    site_id: str | None = None
    expires_at: datetime | None = None
    created_at: datetime


class TokenRotateResponse(BaseModel):
    """Response for POST /v1/admin/tokens/{id}/rotate."""

    token: str
    id: str
    actor_id: str
    label: str
    scopes: list[str]
    site_id: str | None = None
    expires_at: datetime | None = None
    created_at: datetime
    rotated_from: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _token_to_read(link: CapabilityLink, actor: Actor) -> TokenRead:
    return TokenRead(
        id=str(link.id),
        actor_id=str(actor.id),
        label=link.label or actor.label,
        scopes=list(actor.scopes or []),
        site_id=str(actor.site_id) if actor.site_id else None,
        expires_at=link.expires_at,
        last_used_at=actor.last_used_at,
        uses_count=actor.uses_count or 0,
        revoked_at=link.revoked_at,
        created_at=link.created_at,
        ip_allowlist=link.ip_allowlist,
    )


# ---------------------------------------------------------------------------
# POST /v1/admin/tokens — create
# ---------------------------------------------------------------------------


@router.post(
    "",
    summary="Create an API token",
    status_code=201,
    response_model=TokenCreateResponse,
)
def create_token(
    body: TokenCreateRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> TokenCreateResponse:
    """Create a new scoped API token.

    The plaintext is returned ONCE in this response and cannot be retrieved
    again — store it securely.
    """
    # Validate scopes
    invalid = set(body.scopes) - ALL_SCOPES
    if invalid:
        valid = ", ".join(sorted(ALL_SCOPES))
        raise HTTPException(
            status_code=422,
            detail=f"Invalid scopes: {', '.join(sorted(invalid))}. Valid scopes: {valid}",
        )

    # Create the actor (machine type for tokens)
    now = datetime.now(UTC)
    expires_at = None
    if body.expires_in_days is not None:
        expires_at = now + timedelta(days=body.expires_in_days)

    actor = Actor(
        id=uuid.uuid4(),
        kind="machine",
        label=body.label,
        site_id=body.site_id,
        scopes=body.scopes,
        expires_at=expires_at,
    )
    db.add(actor)
    db.flush()

    # Generate and store the token
    plaintext, token_hash = generate_token(actor.id)
    link = CapabilityLink(
        id=uuid.uuid4(),
        actor_id=actor.id,
        token_hash=token_hash,
        label=body.label,
        path_scope="/",
        verbs=["GET", "POST", "PATCH", "DELETE"],
        expires_at=expires_at,
        ip_allowlist=body.ip_allowlist,
    )
    db.add(link)
    db.commit()
    db.refresh(link)
    db.refresh(actor)

    return TokenCreateResponse(
        token=plaintext,
        id=str(link.id),
        actor_id=str(actor.id),
        label=body.label,
        scopes=body.scopes,
        site_id=str(actor.site_id) if actor.site_id else None,
        expires_at=expires_at,
        created_at=link.created_at,
    )


# ---------------------------------------------------------------------------
# GET /v1/admin/tokens — list
# ---------------------------------------------------------------------------


@router.get(
    "",
    summary="List API tokens",
    response_model=list[TokenRead],
)
def list_tokens(
    request: Request,
    db: Session = Depends(get_db),
    include_revoked: bool = False,
) -> list[TokenRead]:
    """List all tokens. Plaintext is never included."""
    from sqlalchemy.orm import joinedload

    query = db.query(CapabilityLink).options(joinedload(CapabilityLink.actor))
    if not include_revoked:
        query = query.filter(CapabilityLink.revoked_at.is_(None))
    links = query.order_by(CapabilityLink.created_at.desc()).all()

    result: list[TokenRead] = []
    for link in links:
        actor = link.actor
        if actor is not None:
            result.append(_token_to_read(link, actor))
    return result


# ---------------------------------------------------------------------------
# DELETE /v1/admin/tokens/{id} — revoke
# ---------------------------------------------------------------------------


@router.delete(
    "/{token_id}",
    summary="Revoke an API token",
)
def revoke_token(
    token_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """Immediately revoke a token. The token is rejected within 10 seconds."""
    link = db.query(CapabilityLink).filter(CapabilityLink.id == token_id).first()
    if link is None:
        raise NotFoundError(
            f"No token with id '{token_id}'.",
            hint="List tokens with GET /v1/admin/tokens to find the right id.",
        )
    link.revoked_at = datetime.now(UTC)
    db.commit()
    return {"status": "revoked", "id": str(token_id)}


# ---------------------------------------------------------------------------
# POST /v1/admin/tokens/{id}/rotate — rotate
# ---------------------------------------------------------------------------


@router.post(
    "/{token_id}/rotate",
    summary="Rotate an API token",
    response_model=TokenRotateResponse,
)
def rotate_token(
    token_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
) -> TokenRotateResponse:
    """Rotate a token: revoke the old one and issue a new one with the same settings."""
    from sqlalchemy.orm import joinedload

    link = (
        db.query(CapabilityLink)
        .options(joinedload(CapabilityLink.actor))
        .filter(CapabilityLink.id == token_id)
        .first()
    )
    if link is None:
        raise NotFoundError(
            f"No token with id '{token_id}'.",
            hint="List tokens with GET /v1/admin/tokens to find the right id.",
        )

    actor = link.actor
    if actor is None:
        raise NotFoundError("Token actor not found.")

    # Revoke the old token immediately
    link.revoked_at = datetime.now(UTC)

    # Create a new token with the same settings
    new_actor = Actor(
        id=uuid.uuid4(),
        kind="machine",
        label=actor.label,
        site_id=actor.site_id,
        scopes=actor.scopes,
        expires_at=actor.expires_at,
    )
    db.add(new_actor)
    db.flush()

    plaintext, token_hash = generate_token(new_actor.id)
    new_link = CapabilityLink(
        id=uuid.uuid4(),
        actor_id=new_actor.id,
        token_hash=token_hash,
        label=link.label,
        path_scope=link.path_scope,
        verbs=link.verbs,
        expires_at=link.expires_at,
        ip_allowlist=link.ip_allowlist,
    )
    db.add(new_link)
    db.commit()
    db.refresh(new_link)
    db.refresh(new_actor)

    return TokenRotateResponse(
        token=plaintext,
        id=str(new_link.id),
        actor_id=str(new_actor.id),
        label=new_actor.label,
        scopes=list(new_actor.scopes or []),
        site_id=str(new_actor.site_id) if new_actor.site_id else None,
        expires_at=new_link.expires_at,
        created_at=new_link.created_at,
        rotated_from=str(token_id),
    )
