"""Request context, rate-limit headers, body-size enforcement (#14) + observability (#24).

Middleware stack (outermost first):

1. ``RequestContextMiddleware`` — request id on every request (#2), byte
   counting and one structured JSON log line per request (#24).
2. ``BodySizeLimitMiddleware`` — streaming body-size reject (#14).
3. ``RateLimitHeadersMiddleware`` — ``X-RateLimit-*`` on every response (#14).
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from typing import Any

from opentelemetry import trace as _otel_trace
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Awaitable, Callable, Receive, Scope, Send

from app.logging import _REQUEST_FIELDS_ATTR, _TOKEN_RE
from app.observability_types import RequestObservation
from app.services.rate_limiter import (
    BODY_SIZE_LIMITS,
    RateLimitResult,
    build_rate_limit_headers,
    check_ip_rate_limit,
)

logger = logging.getLogger("app.request")

otel_use_span = _otel_trace.use_span

REQUEST_ID_HEADER = "X-Request-ID"

# Paths that are exempt from rate limiting (health probes, docs, etc.)
_RATE_LIMIT_EXEMPT_PATHS = frozenset({"/healthz", "/readyz", "/docs", "/redoc", "/openapi.json", "/"})

# Only enforce body-size limits on these methods
_BODY_CHECK_METHODS = frozenset({"POST", "PUT", "PATCH"})

# Matches capability tokens; used to redact untemplated paths in log output.
_CAP_PATH_RE = re.compile(r"/c/[A-Za-z0-9_-]{1,128}/[A-Za-z0-9_-]{20,}")


# ---------------------------------------------------------------------------
# 1. Request context + observability (#2, #24)
# ---------------------------------------------------------------------------


def _safe_path_template(scope: Scope) -> str:
    """Return the route's template path (never the raw path).

    Token-bearing routes are FastAPI ``APIRoute``\\s and always end up with
    ``scope["route"]`` set; for unmatched / rejected-before-routing requests
    we fall back to the literal path only when it cannot contain a token.
    """
    route = scope.get("route")
    template = getattr(route, "path", None) if route is not None else None
    if template:
        return str(template)
    raw_path = scope.get("path", "")
    if _TOKEN_RE.search(raw_path) or _CAP_PATH_RE.search(raw_path):
        return "<unmatched>"
    return raw_path or "<unmatched>"


def _client_ip(scope: Scope) -> str:
    forwarded = None
    for name, value in scope.get("headers", []):
        if name == b"x-forwarded-for":
            forwarded = value.decode("latin-1")
            break
    if forwarded:
        return forwarded.split(",")[0].strip()
    client = scope.get("client")
    return client[0] if client else ""


def _user_agent(scope: Scope) -> str:
    for name, value in scope.get("headers", []):
        if name == b"user-agent":
            return value.decode("latin-1")[:512]
    return ""


def _actor_field(scope: Scope, key: str) -> str:
    return str(scope.get("state", {}).get(key, "")) if isinstance(scope.get("state"), dict) else ""


class RequestContextMiddleware:
    """Raw-ASGI middleware attaching request id and emitting one structured log.

    A pure ASGI middleware (rather than ``BaseHTTPMiddleware``) so bytes in/out
    are counted on the real ``receive``/``send`` stream and the structured
    log line is emitted *after* the response body has been sent.

    ``path_template`` comes from ``request.scope["route"]`` (set by FastAPI's
    ``APIRoute.matches``) so capability tokens never reach the logs (#6, #24).
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = ""
        for name, value in scope.get("headers", []):
            if name == b"x-request-id":
                incoming = value.decode("latin-1").strip()
                break
        request_id = incoming or uuid.uuid4().hex

        scope.setdefault("state", {})["request_id"] = request_id
        scope["state"]["log_fields"] = None  # placeholder, filled at completion

        obs = RequestObservation(request_id=request_id, method=scope.get("method", ""))

        # --- Tracing (#24): extract W3C traceparent + open the root span ------
        from app.observability import tracing

        remote_ctx = tracing.extract_traceparent(
            {k.decode("latin-1"): v.decode("latin-1") for k, v in scope.get("headers", [])}
        )
        span = tracing.start_http_span(obs.method, remote_ctx)

        bytes_in = 0

        async def counting_receive() -> Any:
            nonlocal bytes_in
            message = await receive()
            if message.get("type") == "http.request":
                body = message.get("body", b"")
                bytes_in += len(body)
            return message

        bytes_out = 0
        send_before_app_ok = False
        status_start = [0]

        async def counting_send(message: Any) -> None:
            nonlocal bytes_out, send_before_app_ok
            if message.get("type") == "http.response.start":
                send_before_app_ok = True
                raw_headers = message.get("headers", ())
                if isinstance(raw_headers, (list, tuple)):
                    headers = [
                        tuple(h) if isinstance(h, (list, tuple)) else (h[0], h[1]) for h in raw_headers
                    ]
                else:  # pragma: no cover - defensive
                    headers = list(raw_headers)
                headers = [
                    (k, v)
                    for (k, v) in headers
                    if not (isinstance(k, bytes) and k.lower() == b"x-request-id")
                ]
                resp_id = request_id.encode("latin-1")
                headers.append((REQUEST_ID_HEADER.encode("latin-1"), resp_id))
                # http.response.start must be awaited with the merged headers.
                status_start[0] = int(message.get("status", 0) or 0)
                await send({**{k: v for k, v in message.items() if k != "headers"}, "headers": headers})
                return
            if message.get("type") == "http.response.body" and send_before_app_ok:
                chunk = message.get("body", b"")
                bytes_out += len(chunk) if isinstance(chunk, (bytes, bytearray)) else 0
            await send(message)

        started = time.perf_counter()
        status: int = 0
        try:
            with otel_use_span(span, end_on_exit=False):
                await self.app(scope, counting_receive, counting_send)
            status = status_start[0]
        except BaseException:
            status = status_start[0] or 500
            obs.bytes_err = "exception"
            raise
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000
            obs.path_template = _safe_path_template(scope)
            obs.status = status
            obs.duration_ms = elapsed_ms
            obs.ip = _client_ip(scope)
            obs.user_agent = _user_agent(scope)
            obs.bytes_in = bytes_in
            obs.bytes_out = bytes_out
            obs.actor_id = _actor_field(scope, "actor_id")
            obs.actor_label = _actor_field(scope, "actor_label")
            obs.actor_kind = _actor_field(scope, "actor_kind")
            obs.source = _actor_field(scope, "actor_source") or _actor_field(scope, "source")

            scope["state"]["log_fields"] = obs.as_log_record()

            # Record observables / span end (idempotent, never raises).
            try:
                from app.observability import observe_request

                observe_request(obs)
                tracing.end_http_span(
                    span,
                    status_code=status,
                    duration_ms=elapsed_ms,
                    path_template=obs.path_template,
                    error=obs.is_error,
                    request_id=request_id,
                )
            except Exception:  # pragma: no cover - observability must never break the request
                logger.exception("observability hook failed", extra={"request_id": request_id})

            logger.info(
                "request complete",
                extra={
                    "request_id": request_id,
                    _REQUEST_FIELDS_ATTR: obs.as_log_record(),
                },
            )


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
    _INLINE_MAX_BYTES = 2 * 1024 * 1024  # 2 MB for inline uploads

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
                    # Allow larger payloads for the inline upload endpoint
                    limit = (
                        self._INLINE_MAX_BYTES
                        if "/assets/inline" in request.url.path
                        else self._MAX_BODY_BYTES
                    )
                    if size > limit:
                        from app.errors import problem_response

                        if "/assets/inline" in request.url.path:
                            detail = (
                                f"Inline upload is {size:,} bytes; the maximum is "
                                f"{self._INLINE_MAX_BYTES:,} bytes (2 MB)."
                            )
                            hint = (
                                "Use the presigned upload path for larger files: "
                                "POST /v1/sites/{site}/assets, then PUT to the upload_url."
                            )
                        else:
                            detail = (
                                f"Request body is {size:,} bytes; the maximum is "
                                f"{self._MAX_BODY_BYTES:,} bytes (256 KB)."
                            )
                            hint = (
                                "Reduce the body size and retry. Large posts should be chunked or compressed."
                            )

                        return problem_response(
                            request,
                            status_code=413,
                            code="payload-too-large",
                            title="Payload too large",
                            detail=detail,
                            hint=hint,
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
