"""Typed domain errors (#3).

Services raise these; the HTTP layer turns them into RFC 9457 ``problem+json``
responses (#10 owns the full registry of error types, this module owns the
semantics).  Each error answers three questions for a caller:

* **what** happened (``detail``),
* **how to fix it** (``hint`` — always actionable, never "invalid input"),
* **what to do next** (structured extension members such as ``suggested_slug``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

JsonValue = Any


class DomainError(Exception):
    """Base class for everything a caller can be expected to correct."""

    status_code: int = 500
    code: str = "internal-error"
    title: str = "Internal server error"
    hint: str | None = None
    headers: Mapping[str, str] = {}

    def __init__(
        self,
        detail: str,
        *,
        hint: str | None = None,
        extra: Mapping[str, JsonValue] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        if hint is not None:
            self.hint = hint
        self._extra: dict[str, JsonValue] = dict(extra or {})
        if headers:
            self.headers = dict(headers)

    def extensions(self) -> dict[str, JsonValue]:
        """Extra problem+json members specific to this error."""

        return dict(self._extra)

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"{type(self).__name__}(code={self.code!r}, detail={self.detail!r})"


# --- 400 --------------------------------------------------------------------


class InvalidCursorError(DomainError):
    status_code = 400
    code = "invalid-cursor"
    title = "Invalid pagination cursor"
    hint = (
        "Pass back the opaque `next_cursor` value verbatim, or omit `cursor` "
        "entirely to start from the newest post."
    )


class InvalidQueryError(DomainError):
    status_code = 400
    code = "invalid-query"
    title = "Invalid query parameter"


# --- 404 --------------------------------------------------------------------


class NotFoundError(DomainError):
    status_code = 404
    code = "not-found"
    title = "Not found"


class PostNotFoundError(NotFoundError):
    code = "post-not-found"
    title = "Post not found"

    def __init__(self, identifier: str, *, site: str | None = None) -> None:
        where = f" in site '{site}'" if site else ""
        super().__init__(
            f"No post matches '{identifier}'{where}.",
            hint=(
                "List posts with GET /v1/sites/{site}/posts to find the right id or slug, "
                "or POST a new post — create always returns a draft."
            ),
            extra={"identifier": identifier, "site": site},
        )


class SiteNotFoundError(NotFoundError):
    code = "site-not-found"
    title = "Site not found"

    def __init__(self, slug: str) -> None:
        super().__init__(
            f"No site with slug '{slug}' exists.",
            hint=(
                "Use the demo site created by `make seed` (default slug 'blog'), "
                "or ask an administrator to create the site."
            ),
            extra={"site": slug},
        )


# --- 406 --------------------------------------------------------------------


class NotAcceptableError(DomainError):
    status_code = 406
    code = "not-acceptable"
    title = "Unsupported representation"
    hint = "Request one of: application/json (default), text/markdown, text/html."


# --- 409 --------------------------------------------------------------------


class ConflictError(DomainError):
    status_code = 409
    code = "conflict"
    title = "Conflict"


class SlugConflictError(ConflictError):
    code = "slug-conflict"
    title = "Slug already in use"

    def __init__(self, slug: str, suggested_slug: str, *, site: str | None = None) -> None:
        where = f" in site '{site}'" if site else ""
        super().__init__(
            f"Slug '{slug}' is already taken{where}.",
            hint=(
                "Retry the same request with the free slug in `suggested_slug`. "
                "AgentCMS never overwrites an existing post: PATCH the existing post "
                f"at /v1/posts/{slug} if you meant to update it."
            ),
            extra={"slug": slug, "suggested_slug": suggested_slug, "site": site},
        )


class InvalidTransitionError(ConflictError):
    code = "invalid-transition"
    title = "Status transition not allowed"

    def __init__(
        self, action: str, current_status: str, *, allowed_from: Sequence[str] = (), hint: str | None = None
    ) -> None:
        super().__init__(
            f"Cannot {action} a post whose status is '{current_status}'.",
            hint=hint
            or (
                f"`{action}` is allowed from: {', '.join(allowed_from) or 'no status'}."
                if allowed_from
                else None
            ),
            extra={
                "action": action,
                "current_status": current_status,
                "allowed_from": list(allowed_from),
            },
        )


class RevisionConflictError(ConflictError):
    """Raised when an If-Match revision guard fails (#11)."""

    code = "revision-conflict"
    title = "Revision conflict"

    def __init__(self, expected: str, actual: str) -> None:
        super().__init__(
            f"If-Match {expected} does not match the current revision {actual}.",
            hint="Re-read the post, merge your change, and retry with the current revision.",
            extra={"expected_revision": expected, "current_revision": actual},
        )


# --- 422 --------------------------------------------------------------------


class UnprocessableContentError(DomainError):
    status_code = 422
    code = "unprocessable-content"
    title = "Content cannot be processed"


class ContentRequiredError(UnprocessableContentError):
    code = "content-required"
    title = "Document body required"
    hint = "Send `body_md` with the Markdown body — it is the only required field on create."


class TitleRequiredError(UnprocessableContentError):
    code = "title-required"
    title = "Title required"

    def __init__(self) -> None:
        super().__init__(
            "Neither `title` nor a first-level heading in `body_md` was found.",
            hint=(
                "Send `title`, or start `body_md` with a single '# Heading' line — "
                "the title is derived from the first H1 (and used to derive the slug) "
                "when `title` is omitted."
            ),
        )


class SlugInvalidError(UnprocessableContentError):
    code = "slug-invalid"
    title = "Slug is not usable"

    def __init__(self, slug: str, reason: str) -> None:
        super().__init__(
            f"Slug '{slug}' is not usable: {reason}.",
            hint=(
                "Slugs contain lowercase letters, digits and single hyphens. "
                "Omit `slug` to derive it from the title."
            ),
            extra={"slug": slug, "reason": reason},
        )


# --- 503 --------------------------------------------------------------------


class DatabaseUnavailableError(DomainError):
    status_code = 503
    code = "database-unavailable"
    title = "Database unavailable"

    def __init__(self, detail: str = "The database is not reachable.") -> None:
        super().__init__(
            detail,
            hint=(
                "Retry with a short backoff. If it persists, check DATABASE_URL "
                "and that Postgres is up (`make up`)."
            ),
            headers={"Retry-After": "2"},
        )
