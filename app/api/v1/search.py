"""Search endpoint (#19).

GET /v1/search?q=&site=&status=&tag=&author_label=&from=&to=&limit=&cursor=

Full-text search with weighted tsvector, faceted filters, cursor pagination,
and optional ``?format=ids`` for cheap "have I written this?" checks.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.auth import AuthContext, require_auth
from app.db.session import get_db
from app.services.search import search_posts

from .schemas import SearchResponse

router = APIRouter()

DbSession = Annotated[Session, Depends(get_db)]


@router.get(
    "/search",
    summary="Full-text search across posts",
    tags=["search"],
    response_model=SearchResponse,
)
def search_endpoint(
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_auth),
    q: str | None = Query(None, description="Full-text search query"),
    site: str | None = Query(None, description="Filter by site slug"),
    status: str | None = Query(None, description="Filter by status"),
    tag: str | None = Query(None, description="Filter by tag slug"),
    author_label: str | None = Query(None, description="Filter by author label"),
    from_date: str | None = Query(None, alias="from", description="Created after (ISO-8601)"),
    to_date: str | None = Query(None, alias="to", description="Created before (ISO-8601)"),
    published_after: str | None = Query(None, description="Published after (ISO-8601)"),
    published_before: str | None = Query(None, description="Published before (ISO-8601)"),
    updated_since: str | None = Query(None, description="Updated since (ISO-8601)"),
    limit: int = Query(20, ge=1, le=100),
    cursor: str | None = Query(None, description="Pagination cursor"),
    format: str | None = Query(None, description="Response format: 'ids' for lightweight results"),
) -> dict:
    """Full-text search with faceted filters.

    Empty ``q`` behaves like list-with-filters (no 400).
    ``?format=ids`` returns just id/slug pairs — the cheapest possible
    "have I written this?" call for an agent.
    """
    from datetime import datetime

    parsed_from = datetime.fromisoformat(from_date) if from_date else None
    parsed_to = datetime.fromisoformat(to_date) if to_date else None
    parsed_published_after = datetime.fromisoformat(published_after) if published_after else None
    parsed_published_before = datetime.fromisoformat(published_before) if published_before else None
    parsed_updated_since = datetime.fromisoformat(updated_since) if updated_since else None

    result = search_posts(
        db,
        q=q,
        site_slug=site,
        status=status,
        tag=tag,
        author_label=author_label,
        from_date=parsed_from,
        to_date=parsed_to,
        published_after=parsed_published_after,
        published_before=parsed_published_before,
        updated_since=parsed_updated_since,
        limit=limit,
        cursor=cursor,
        format_ids=(format == "ids"),
    )

    return {
        "results": result["results"],
        "total_estimate": result["total_estimate"],
        "next_cursor": result["next_cursor"],
        "query": q,
    }
