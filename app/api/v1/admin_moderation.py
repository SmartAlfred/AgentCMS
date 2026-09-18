"""Admin moderation endpoints (#17).

Activity → Security dashboard:
* ``GET  /v1/admin/moderation/flagged``  — list flagged items
* ``POST /v1/admin/moderation/decisions/{id}/clear``  — dismiss a flag
* ``POST /v1/admin/moderation/decisions/{id}/block``  — escalate to block
* ``POST /v1/admin/moderation/decisions/{id}/clear-and-allow-similar``  — clear and allow similar content
* ``GET  /v1/admin/content-policies``  — list content policies for a site
* ``POST /v1/admin/content-policies``  — create a content policy
* ``DELETE /v1/admin/content-policies/{id}``  — delete a content policy
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.auth import AuthContext, require_auth
from app.db.session import get_db
from app.models.content_policy import ContentPolicy

router = APIRouter(prefix="/admin", tags=["admin", "moderation"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class ModerationDecisionRead(BaseModel):
    """A moderation decision in the Security view."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    post_id: str
    rule_name: str
    rule_kind: str
    outcome: str
    evidence: str | None = None
    matched_post_id: str | None = None
    cleared: bool
    cleared_by_actor_id: str | None = None
    cleared_at: Any = None
    blocked: bool
    created_at: Any


class ModerationDecisionListResponse(BaseModel):
    """Cursor-paginated list of moderation decisions."""

    model_config = ConfigDict(from_attributes=True)

    items: list[ModerationDecisionRead]
    next_cursor: str | None = None
    count: int


class ContentPolicyCreate(BaseModel):
    """Request body for POST /v1/admin/content-policies."""

    model_config = ConfigDict(str_strip_whitespace=True)

    site_id: str
    name: str
    kind: str
    severity: str = "flag"
    config: dict[str, Any] | None = None
    enabled: bool = True
    priority: int = 0


class ContentPolicyRead(BaseModel):
    """A content policy rule."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    site_id: str
    name: str
    kind: str
    severity: str
    config: dict[str, Any] | None = None
    enabled: bool
    priority: int
    created_at: Any


class ContentPolicyListResponse(BaseModel):
    """List of content policies for a site."""

    model_config = ConfigDict(from_attributes=True)

    items: list[ContentPolicyRead]
    count: int


# ---------------------------------------------------------------------------
# Flagged items
# ---------------------------------------------------------------------------


@router.get(
    "/moderation/flagged",
    summary="List flagged moderation decisions",
    response_model=ModerationDecisionListResponse,
)
def list_flagged(
    request: Request,
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(require_auth),
    site_id: str | None = Query(None, description="Filter by site UUID"),
    cleared: bool | None = Query(None, description="Filter by cleared status"),
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = Query(None),
) -> ModerationDecisionListResponse:
    from app.services.content_policy import list_flagged_decisions

    parsed_site_id = uuid.UUID(site_id) if site_id else None
    items, next_cursor = list_flagged_decisions(
        db,
        site_id=parsed_site_id,
        cleared=cleared,
        limit=limit,
        cursor=cursor,
    )
    return ModerationDecisionListResponse(
        items=[
            ModerationDecisionRead(
                id=str(d.id),
                post_id=str(d.post_id),
                rule_name=d.rule_name,
                rule_kind=d.rule_kind,
                outcome=d.outcome,
                evidence=d.evidence,
                matched_post_id=str(d.matched_post_id) if d.matched_post_id else None,
                cleared=d.cleared,
                cleared_by_actor_id=str(d.cleared_by_actor_id) if d.cleared_by_actor_id else None,
                cleared_at=d.cleared_at,
                blocked=d.blocked,
                created_at=d.created_at,
            )
            for d in items
        ],
        next_cursor=next_cursor,
        count=len(items),
    )


# ---------------------------------------------------------------------------
# Clear a decision
# ---------------------------------------------------------------------------


@router.post(
    "/moderation/decisions/{decision_id}/clear",
    summary="Clear a flagged decision (human action)",
)
def clear_decision_endpoint(
    decision_id: str,
    request: Request,
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(require_auth),
) -> dict[str, Any]:
    from app.services.content_policy import clear_decision

    decision = clear_decision(db, uuid.UUID(decision_id), actor_id=auth.actor_id)

    # Audit
    from app.services.audit import record_event

    record_event(
        db,
        action="moderation.cleared",
        actor_id=auth.actor_id,
        actor_label=auth.label,
        actor_kind="human",
        source="dashboard",
        target_type="moderation_decision",
        target_id=str(decision.id),
        event_metadata={
            "rule_name": decision.rule_name,
            "post_id": str(decision.post_id),
        },
    )
    db.commit()

    return {
        "status": "cleared",
        "decision_id": str(decision.id),
        "message": f"Decision '{decision.rule_name}' has been cleared.",
    }


# ---------------------------------------------------------------------------
# Escalate to block
# ---------------------------------------------------------------------------


@router.post(
    "/moderation/decisions/{decision_id}/block",
    summary="Escalate a flagged decision to blocked",
)
def block_decision_endpoint(
    decision_id: str,
    request: Request,
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(require_auth),
) -> dict[str, Any]:
    from app.services.content_policy import block_decision

    decision = block_decision(db, uuid.UUID(decision_id))

    # Audit
    from app.services.audit import record_event

    record_event(
        db,
        action="moderation.blocked",
        actor_id=auth.actor_id,
        actor_label=auth.label,
        actor_kind="human",
        source="dashboard",
        target_type="moderation_decision",
        target_id=str(decision.id),
        event_metadata={
            "rule_name": decision.rule_name,
            "post_id": str(decision.post_id),
        },
    )
    db.commit()

    return {
        "status": "blocked",
        "decision_id": str(decision.id),
        "message": f"Decision '{decision.rule_name}' has been escalated to block.",
    }


# ---------------------------------------------------------------------------
# Clear and allow similar
# ---------------------------------------------------------------------------


@router.post(
    "/moderation/decisions/{decision_id}/clear-and-allow-similar",
    summary="Clear decision and add exception for similar content",
)
def clear_and_allow_similar_endpoint(
    decision_id: str,
    request: Request,
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(require_auth),
) -> dict[str, Any]:
    """Clear the decision and disable the specific rule for this site."""
    from app.services.content_policy import clear_decision

    decision = clear_decision(db, uuid.UUID(decision_id), actor_id=auth.actor_id)

    # Disable the policy rule that triggered this decision
    if decision.policy_id:
        policy = db.query(ContentPolicy).filter(ContentPolicy.id == decision.policy_id).first()
        if policy:
            policy.enabled = False
            db.flush()

    # Audit
    from app.services.audit import record_event

    record_event(
        db,
        action="moderation.cleared",
        actor_id=auth.actor_id,
        actor_label=auth.label,
        actor_kind="human",
        source="dashboard",
        target_type="moderation_decision",
        target_id=str(decision.id),
        event_metadata={
            "rule_name": decision.rule_name,
            "post_id": str(decision.post_id),
            "allow_similar": True,
            "policy_disabled": str(decision.policy_id) if decision.policy_id else None,
        },
    )
    db.commit()

    return {
        "status": "cleared",
        "decision_id": str(decision.id),
        "policy_disabled": str(decision.policy_id) if decision.policy_id else None,
        "message": "Decision cleared and similar content will be allowed.",
    }


# ---------------------------------------------------------------------------
# Content policy CRUD
# ---------------------------------------------------------------------------


@router.get(
    "/content-policies",
    summary="List content policies for a site",
    response_model=ContentPolicyListResponse,
)
def list_content_policies(
    request: Request,
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(require_auth),
    site_id: str = Query(..., description="Site UUID"),
) -> ContentPolicyListResponse:
    parsed_site_id = uuid.UUID(site_id)
    policies = (
        db.query(ContentPolicy)
        .filter(ContentPolicy.site_id == parsed_site_id)
        .order_by(ContentPolicy.priority.asc(), ContentPolicy.created_at.asc())
        .all()
    )
    return ContentPolicyListResponse(
        items=[
            ContentPolicyRead(
                id=str(p.id),
                site_id=str(p.site_id),
                name=p.name,
                kind=p.kind,
                severity=p.severity,
                config=p.config,
                enabled=p.enabled,
                priority=p.priority,
                created_at=p.created_at,
            )
            for p in policies
        ],
        count=len(policies),
    )


@router.post(
    "/content-policies",
    summary="Create a content policy rule",
    status_code=201,
)
def create_content_policy(
    body: ContentPolicyCreate,
    request: Request,
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(require_auth),
) -> dict[str, Any]:
    policy = ContentPolicy(
        id=uuid.uuid4(),
        site_id=uuid.UUID(body.site_id),
        name=body.name,
        kind=body.kind,
        severity=body.severity,
        config=body.config or {},
        enabled=body.enabled,
        priority=body.priority,
    )
    db.add(policy)
    db.commit()
    return {
        "id": str(policy.id),
        "name": policy.name,
        "kind": policy.kind,
        "severity": policy.severity,
        "message": f"Content policy '{policy.name}' created.",
    }


@router.delete(
    "/content-policies/{policy_id}",
    summary="Delete a content policy rule",
)
def delete_content_policy(
    policy_id: str,
    request: Request,
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(require_auth),
) -> dict[str, Any]:
    policy = db.query(ContentPolicy).filter(ContentPolicy.id == uuid.UUID(policy_id)).first()
    if policy is None:
        from app.domain.errors import NotFoundError

        raise NotFoundError(f"Content policy '{policy_id}' not found.")
    name = policy.name
    db.delete(policy)
    db.commit()
    return {
        "status": "deleted",
        "message": f"Content policy '{name}' deleted.",
    }
