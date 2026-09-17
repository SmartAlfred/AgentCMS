"""Request context, rate-limit headers, body-size enforcement (#14).

Middleware stack (outermost first):

1. ``RequestContextMiddleware`` — request id on every request (#2).
2. ``BodySizeLimitMiddleware`` — streaming body-size reject (#14).
3. ``RateLimitHeadersMiddleware`` — ``X-RateLimit-*`` on every response (#14).
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from app.services.rate_limiter import (
    BODY_SIZE_LIMITS,
    RateLimitResult,
    build_rate_limit_headers,
    check_ip_rate_limit,
)

logger = logging.getLogger("app.request")

REQUEST_ID_HEADER = "X-Request-ID"

# Paths that are exempt from rate limiting (health probes, docs, etc.)
_RATE_LIMIT_EXEMPT_PATHS = frozenset({"/healthz", "/readyz", "/docs", "/redoc", "/openapi.json", "/"})

# Only enforce body-size limits on these methods
_BODY_CHECK_METHODS = frozenset({"POST", "PUT", "PATCH"})


# ---------------------------------------------------------------------------
# 1. Request context (unchanged from #2)
# ---------------------------------------------------------------------------


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Attach ``request.state.request_id`` and echo it back in ``X-Request-ID``."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        incoming = request.headers.get(REQUEST_ID_HEADER, "")
        request_id = incoming.strip() or uuid.uuid4().hex
        request.state.request_id = request_id
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:  # pragma: no cover - defensive, handlers normally catch
            logger.exception(
                "unhandled error %s %s",
                request.method,
                request.url.path,
                extra={"request_id": request_id},
            )
            raise
        elapsed_ms = (time.perf_counter() - started) * 1000
        response.headers[REQUEST_ID_HEADER] = request_id
        logger.info(
            "%s %s -> %s in %.1fms",
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
            extra={"request_id": request_id},
        )
        return response


# ---------------------------------------------------------------------------
# 2. Body-size enforcement (streaming reject)
# ---------------------------------------------------------------------------


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject requests whose ``Content-Length`` exceeds the body-size limit.

    Enforces *before* the body is fully buffered, so oversized payloads
    are rejected cheaply.  The check is on the declared ``Content-Length``
    header — if absent, the body is read up to the limit by downstream code.
    """

    _MAX_BODY_BYTES = BODY_SIZE_LIMITS["post_body_bytes"]  # 256 KB

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.method in _BODY_CHECK_METHODS:
            content_length = request.headers.get("content-length")
            if content_length is not None:
                try:
                    size = int(content_length)
                except ValueError:
                    pass
                else:
                    if size > self._MAX_BODY_BYTES:
                        from app.errors import problem_response

                        return problem_response(
                            request,
                            status_code=413,
                            code="payload-too-large",
                            title="Payload too large",
                            detail=(
                                f"Request body is {size:,} bytes; the maximum is "
                                f"{self._MAX_BODY_BYTES:,} bytes (256 KB)."
                            ),
                            hint=(
                                "Reduce the body size and retry. Large posts should be chunked or compressed."
                            ),
                        )
        return await call_next(request)


# ---------------------------------------------------------------------------
# 3. Rate-limit headers on every response
# ---------------------------------------------------------------------------


_RATE_LIMIT_HEADER_RE = re.compile(r"^X-RateLimit-", re.IGNORECASE)


class RateLimitHeadersMiddleware(BaseHTTPMiddleware):
    """Attach ``X-RateLimit-*`` headers to every response.

    For authenticated routes, the rate-limit result is stored on
    ``request.state.rate_limit_result`` by the ``require_auth`` dependency
    (or the rate-limit dependency).  This middleware reads it and adds headers.

    For unauthenticated routes on the public surface, IP-based rate limiting
    is applied here.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # Skip exempt paths (health, docs)
        if request.url.path in _RATE_LIMIT_EXEMPT_PATHS:
            return await call_next(request)

        # Call the downstream app first — the auth dependency sets
        # request.state.rate_limit_result during this call.
        response = await call_next(request)

        # Read the rate-limit result set by the auth dependency
        rl_result: RateLimitResult | None = getattr(request.state, "rate_limit_result", None)

        # For unauthenticated paths (public surface), apply IP-based limiting
        if rl_result is None and not _has_auth_header(request):
            rl_result = _apply_ip_rate_limit(request)

        # Attach headers
        if rl_result is not None:
            headers = build_rate_limit_headers(rl_result)
            for k, v in headers.items():
                response.headers[k] = v

        return response


def _has_auth_header(request: Request) -> bool:
    """Return True if the request carries an Authorization header."""
    return "authorization" in request.headers


def _apply_ip_rate_limit(request: Request) -> RateLimitResult | None:
    """Apply IP-based rate limit for unauthenticated requests.

    Returns the result if applied, None if exempt.
    """
    # Only apply to the public read surface and agent-docs routes
    # Public routes: /{site}/{slug}, /{site}, /c/{token}, etc.
    # Agent-docs: /llms.txt, /, /v1/discover, /changelog
    # These are all GET requests for reads
    if request.method != "GET":
        return None

    client_ip = _get_client_ip(request)
    if client_ip is None:
        return None

    result = check_ip_rate_limit(client_ip, bucket="reads")
    return result


def _get_client_ip(request: Request) -> str | None:
    """Extract the client IP, respecting X-Forwarded-For."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return None
