"""RFC 9457 ``application/problem+json`` responses (#2, extended by #10).

One shape for every failure, so an agent can handle errors with a single
branch and still learn *how* to fix it::

    {
      "type":     "https://agentcms.dev/problems/slug-conflict",
      "title":    "Slug already in use",
      "status":   409,
      "detail":   "Slug 'hello' is already taken in site 'blog'.",
      "instance": "/v1/sites/blog/posts",
      "code":     "slug-conflict",
      "hint":     "Retry the same request with the free slug in `suggested_slug`...",
      "suggested_slug": "hello-2",
      "request_id": "0f0c…"
    }
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.domain.errors import DomainError

logger = logging.getLogger("app.errors")

PROBLEM_MEDIA_TYPE = "application/problem+json"
PROBLEM_TYPE_BASE = "https://agentcms.dev/problems/"

# Capability tokens are bearer secrets: they must never be echoed back in a body.
_CAP_TOKEN_RE = re.compile(r"cap_[A-Za-z0-9_\-]+")


def _redact_instance(path: str) -> str:
    """Redact capability tokens out of the RFC 9457 `instance` member."""
    return _CAP_TOKEN_RE.sub("cap_…***", path)


_BLANK_BROWSER_HINT = (
    "See GET /docs for the interactive reference, or GET /openapi.json for the machine-readable contract."
)


def problem_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    title: str,
    detail: str,
    hint: str | None = None,
    extra: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """Build a problem+json response, always carrying the request id."""

    body: dict[str, Any] = {
        "type": f"{PROBLEM_TYPE_BASE}{code}",
        "title": title,
        "status": status_code,
        "detail": detail,
        "instance": _redact_instance(request.url.path),
        "code": code,
        "request_id": getattr(request.state, "request_id", "-"),
    }
    if hint:
        body["hint"] = hint
    if extra:
        body.update(extra)
    return JSONResponse(
        status_code=status_code,
        content=body,
        media_type=PROBLEM_MEDIA_TYPE,
        headers=dict(headers or {}),
    )


def register_error_handlers(app: FastAPI) -> None:
    """Make every error path return problem+json."""

    @app.exception_handler(DomainError)
    async def _domain_error(request: Request, exc: DomainError) -> JSONResponse:
        return problem_response(
            request,
            status_code=exc.status_code,
            code=exc.code,
            title=exc.title,
            detail=exc.detail,
            hint=exc.hint,
            extra=exc.extensions(),
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors: list[dict[str, Any]] = []
        is_json_parse = False
        for error in exc.errors():
            entry: dict[str, Any] = {
                "field": ".".join(str(part) for part in error.get("loc", ()) if part != "body"),
                "code": _VALIDATION_CODE_MAP.get(error.get("type", ""), "invalid"),
                "message": error.get("msg", ""),
                "type": error.get("type", ""),
            }
            ctx = error.get("ctx", {})
            if error.get("type") == "json_invalid":
                is_json_parse = True
                # Surface the JSON decode error detail and byte offset
                if "error" in ctx:
                    entry["detail"] = str(ctx["error"])
                # byte offset: loc[1] holds the position for json_invalid errors
                loc = error.get("loc", ())
                if len(loc) > 1:
                    entry["byte_offset"] = loc[1]
            for k, v in ctx.items():
                if not isinstance(v, Exception):
                    entry[k] = v
            errors.append(entry)

        if is_json_parse:
            return problem_response(
                request,
                status_code=400,
                code="json-parse-error",
                title="Malformed request body",
                detail="The request body is not valid JSON.",
                hint=(
                    "Ensure the body is well-formed JSON and retry. "
                    "Truncated or syntactically broken JSON causes this error."
                ),
                extra={"errors": errors},
            )

        return problem_response(
            request,
            status_code=422,
            code="validation-error",
            title="Request validation failed",
            detail="The request body or query string does not match the documented contract.",
            hint=(
                "Fix the fields listed in `errors`, then retry. "
                "GET /openapi.json has the exact schema for this endpoint."
            ),
            extra={"errors": errors},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _HTTP_CODES.get(exc.status_code, "http-error")
        return problem_response(
            request,
            status_code=exc.status_code,
            code=code,
            title=_HTTP_TITLES.get(exc.status_code, "HTTP error"),
            detail=str(exc.detail) if exc.detail else _HTTP_TITLES.get(exc.status_code, "HTTP error"),
            hint=_HTTP_HINTS.get(exc.status_code, _BLANK_BROWSER_HINT),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled exception on %s %s", request.method, request.url.path)
        return problem_response(
            request,
            status_code=500,
            code="internal-error",
            title="Internal server error",
            detail="The server hit an unexpected condition while handling this request.",
            hint=(
                "Retry once; if it repeats, report the `request_id` above — "
                "it maps to the server-side traceback."
            ),
        )


_VALIDATION_CODE_MAP: dict[str, str] = {
    "json_invalid": "json-parse-error",
    "missing": "missing-field",
    "string_type": "invalid-type",
    "int_type": "invalid-type",
    "list_type": "invalid-type",
    "bool_type": "invalid-type",
    "value_error": "invalid-value",
    "string_too_long": "value-too-long",
    "string_too_short": "value-too-short",
    "int_too_large": "value-out-of-range",
    "int_too_small": "value-out-of-range",
    "enum": "invalid-enum-value",
    "extra_forbidden": "unexpected-field",
}

_HTTP_TITLES: dict[int, str] = {
    400: "Bad request",
    401: "Unauthenticated",
    403: "Forbidden",
    404: "Endpoint not found",
    405: "Method not allowed",
    406: "Unsupported representation",
    409: "Conflict",
    413: "Payload too large",
    415: "Unsupported media type",
    422: "Request validation failed",
    429: "Too many requests",
    500: "Internal server error",
    503: "Service unavailable",
}

_HTTP_CODES: dict[int, str] = {
    400: "bad-request",
    401: "unauthenticated",
    403: "forbidden",
    404: "endpoint-not-found",
    405: "method-not-allowed",
    406: "not-acceptable",
    409: "conflict",
    413: "payload-too-large",
    415: "unsupported-media-type",
    422: "validation-error",
    429: "rate-limited",
    500: "internal-error",
    503: "service-unavailable",
}

_HTTP_HINTS: dict[int, str] = {
    401: (
        "Present a scoped API token as `Authorization: Bearer <token>`, or use "
        "the capability link you were given (see #6)."
    ),
    403: "The token or link you used does not carry the scope this call needs.",
    404: "Check the path against GET /openapi.json — paths are versioned under /v1.",
    405: "Check the HTTP method for this path in GET /openapi.json.",
    429: "Respect `Retry-After` and slow down; see #14 for quota details.",
}
