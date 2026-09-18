"""Webhook admin API routes (#21).

CRUD endpoints for webhook subscriptions, plus test ping and redeliver.
All routes require admin auth (posts:write scope).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.auth import AuthContext, require_auth
from app.db.session import get_db

router = APIRouter()
DbSession = Annotated[Session, Depends(get_db)]


# ---------------------------------------------------------------------------
# Request/Response schemas
# ---------------------------------------------------------------------------


class WebhookCreate(BaseModel):
    """Request body for POST /v1/admin/webhooks."""

    model_config = ConfigDict(str_strip_whitespace=True)

    url: str
    events: list[str] | None = None
    site: str | None = None
    active: bool = True


class WebhookUpdate(BaseModel):
    """Request body for PATCH /v1/admin/webhooks/{id}."""

    model_config = ConfigDict(str_strip_whitespace=True)

    url: str | None = None
    events: list[str] | None = None
    active: bool | None = None


class WebhookRead(BaseModel):
    """Webhook response (secret is only shown on create)."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    url: str
    events: list[str] = Field(default_factory=list)
    active: bool
    site: str | None = None
    created_at: str | None = None


class WebhookCreateResponse(BaseModel):
    """Response from POST /v1/admin/webhooks — includes secret shown once."""

    id: str
    url: str
    secret: str
    events: list[str] = Field(default_factory=list)
    active: bool
    site: str | None = None
    created_at: str | None = None


class DeliveryRead(BaseModel):
    """Webhook delivery record."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    event: str
    response_status: int | None = None
    response_body: str | None = None
    delivered_at: str | None = None
    created_at: str | None = None


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


@router.post(
    "/admin/webhooks",
    summary="Create a webhook subscription",
    tags=["webhooks"],
    status_code=201,
)
def create_webhook_endpoint(
    body: WebhookCreate,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_auth),
) -> WebhookCreateResponse:
    from app.services.webhook import create_webhook

    webhook, secret = create_webhook(
        db,
        url=body.url,
        event_types=body.events,
        site_slug=body.site,
        active=body.active,
    )
    db.commit()

    return WebhookCreateResponse(
        id=str(webhook.id),
        url=webhook.url,
        secret=secret,
        events=webhook.events or [],
        active=webhook.active,
        site=body.site,
        created_at=webhook.created_at.isoformat() if webhook.created_at else None,
    )


@router.get(
    "/admin/webhooks",
    summary="List webhook subscriptions",
    tags=["webhooks"],
)
def list_webhooks_endpoint(
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_auth),
    site: str | None = Query(None, description="Filter by site slug"),
    active_only: bool = Query(False, description="Only show active webhooks"),
) -> list[WebhookRead]:
    from app.services.webhook import list_webhooks

    webhooks = list_webhooks(db, site_slug=site, active_only=active_only)
    return [
        WebhookRead(
            id=str(wh.id),
            url=wh.url,
            events=wh.events or [],
            active=wh.active,
            created_at=wh.created_at.isoformat() if wh.created_at else None,
        )
        for wh in webhooks
    ]


@router.get(
    "/admin/webhooks/{webhook_id}",
    summary="Get a webhook",
    tags=["webhooks"],
)
def get_webhook_endpoint(
    webhook_id: str,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_auth),
) -> WebhookRead:
    from app.services.webhook import get_webhook

    webhook = get_webhook(db, webhook_id)
    return WebhookRead(
        id=str(webhook.id),
        url=webhook.url,
        events=webhook.events or [],
        active=webhook.active,
        created_at=webhook.created_at.isoformat() if webhook.created_at else None,
    )


@router.patch(
    "/admin/webhooks/{webhook_id}",
    summary="Update a webhook",
    tags=["webhooks"],
)
def update_webhook_endpoint(
    webhook_id: str,
    body: WebhookUpdate,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_auth),
) -> WebhookRead:
    from app.services.webhook import update_webhook

    webhook = update_webhook(
        db,
        webhook_id,
        url=body.url,
        event_types=body.events,
        active=body.active,
    )
    db.commit()

    return WebhookRead(
        id=str(webhook.id),
        url=webhook.url,
        events=webhook.events or [],
        active=webhook.active,
        created_at=webhook.created_at.isoformat() if webhook.created_at else None,
    )


@router.delete(
    "/admin/webhooks/{webhook_id}",
    summary="Delete a webhook",
    tags=["webhooks"],
    status_code=204,
)
def delete_webhook_endpoint(
    webhook_id: str,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_auth),
) -> Response:
    from app.services.webhook import delete_webhook

    delete_webhook(db, webhook_id)
    db.commit()
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Test & redeliver
# ---------------------------------------------------------------------------


@router.post(
    "/admin/webhooks/{webhook_id}/test",
    summary="Send a signed test ping",
    tags=["webhooks"],
)
def test_webhook_endpoint(
    webhook_id: str,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_auth),
) -> dict[str, Any]:
    from app.services.webhook import send_test_ping

    delivery = send_test_ping(db, webhook_id)
    db.commit()
    return {
        "delivery_id": str(delivery.id),
        "status": delivery.response_status,
        "delivered_at": delivery.delivered_at.isoformat() if delivery.delivered_at else None,
    }


@router.post(
    "/admin/webhooks/{webhook_id}/redeliver/{delivery_id}",
    summary="Redeliver a specific delivery",
    tags=["webhooks"],
)
def redeliver_endpoint(
    webhook_id: str,
    delivery_id: str,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_auth),
) -> dict[str, Any]:
    from app.services.webhook import redeliver

    delivery = redeliver(db, webhook_id, delivery_id)
    db.commit()
    return {
        "delivery_id": str(delivery.id),
        "status": delivery.response_status,
        "delivered_at": delivery.delivered_at.isoformat() if delivery.delivered_at else None,
    }
