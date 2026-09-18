"""OpenTelemetry tracing (#24).

Wires an OpenTelemetry ``TracerProvider``:
- default sampling ``trace_sample_ratio`` (10%) for root spans,
- 100% for error spans (recorded via an always-on error tracer),
- W3C Trace-Context extraction from inbound requests and injection into
  outbound webhook calls (``traceparent``).

When no OTLP endpoint is configured the providers are no-op (zero cost), so
the same span calls are safe in tests and production alike.  Tests override
the exporter with an ``InMemorySpanExporter`` via
:func:`install_test_exporter`.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Iterator, Sequence
from typing import Any

from opentelemetry import trace as otel_trace
from opentelemetry.context import Context
from opentelemetry.propagate import extract, inject
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import (
    Decision,
    ParentBased,
    Sampler,
    SamplingResult,
    TraceIdRatioBased,
)
from opentelemetry.trace import INVALID_SPAN, Link, Span, SpanKind, StatusCode, Tracer
from opentelemetry.util.types import Attributes

from app.config import Settings

_provider: TracerProvider | None = None
_error_provider: TracerProvider | None = None
_tracer: Tracer | None = None
_error_tracer: Tracer | None = None
_exporter: Any = None
_global_provider_set = False

# Thread-local stack of spans so that sync service code can open/close spans
# without threading ``with`` blocks through every call site.
_tls = threading.local()


def _reset() -> None:
    global _provider, _error_provider, _tracer, _error_tracer, _exporter, _global_provider_set
    _provider = None
    _error_provider = None
    _tracer = None
    _error_tracer = None
    _exporter = None
    _global_provider_set = False
    _tls.spans = []


def configure_tracing(settings: Settings) -> None:
    """Build the tracer providers from settings (idempotent)."""
    global _provider, _error_provider, _tracer, _error_tracer, _exporter
    ratio = settings.trace_sample_ratio
    sample_resource = Resource.create({"service.name": settings.otel_service_name or "agentcms"})

    # Main provider: ratio sampling for root spans, ParentBased downstream.
    _provider = TracerProvider(resource=sample_resource, sampler=_AgentSampler(ratio))
    _error_provider = TracerProvider(resource=sample_resource)  # AlwaysOn default

    if settings.otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        try:
            _exporter = OTLPSpanExporter(endpoint=settings.otlp_endpoint)
            _provider.add_span_processor(BatchSpanProcessor(_exporter))
            _error_provider.add_span_processor(BatchSpanProcessor(_exporter))
        except Exception:
            _provider = TracerProvider(resource=sample_resource, sampler=_AgentSampler(ratio))

    _tracer = _provider.get_tracer("agentcms")
    _error_tracer = _error_provider.get_tracer("agentcms-errors")

    # The SDK forbids overriding the global provider; once it is set we keep
    # the module-level providers as-is to avoid spurious warnings in tests.
    global _global_provider_set
    if not _global_provider_set:
        otel_trace.set_tracer_provider(_provider)
        _global_provider_set = True


class _AgentSampler(Sampler):
    """Ratio sampler for root spans; errors are escalated by the error tracer."""

    def __init__(self, ratio: float) -> None:
        self._ratio_sampler: ParentBased = ParentBased(TraceIdRatioBased(ratio))

    def should_sample(
        self,
        parent_context: Context | None,
        trace_id: int,
        name: str,
        kind: SpanKind | None = None,
        attributes: Attributes = None,
        links: Sequence[Link] | None = None,
        trace_state: Any = None,
    ) -> SamplingResult:
        # Named error roots are always sampled.
        if (name and name.endswith(".error")) or (attributes or {}).get("error"):
            return SamplingResult(Decision.RECORD_AND_SAMPLE, attributes)
        return self._ratio_sampler.should_sample(parent_context, trace_id, name, kind, attributes, links)

    def get_description(self) -> str:
        return f"_AgentSampler(ratio) -> {self._ratio_sampler.get_description()}"


@contextlib.contextmanager
def trace(name: str, attributes: dict[str, Any] | None = None) -> Iterator[Span | None]:
    """Context manager opening a child span under the current trace context.

    If no provider is configured, this is a no-op (zero-cost).
    """
    if _tracer is None:
        yield None
        return
    stack = _tls.spans if hasattr(_tls, "spans") else []
    span = _tracer.start_span(name, attributes=attributes, kind=otel_trace.SpanKind.INTERNAL)
    stack.append(span)
    try:
        with otel_trace.use_span(span, end_on_exit=False):
            yield span
    finally:
        span.end()
        stack.pop()


def start_span(name: str, attributes: dict[str, Any] | None = None) -> Span:
    """Manual span start; pair with :func:`end_span`."""
    if _tracer is None:
        return INVALID_SPAN
    span = _tracer.start_span(name, attributes=attributes, kind=otel_trace.SpanKind.INTERNAL)
    stack = getattr(_tls, "spans", None)
    if stack is None:
        stack = []
        _tls.spans = stack
    stack.append(span)
    return span


def start_http_span(
    method: str,
    remote_context: Any = None,
    attributes: dict[str, Any] | None = None,
) -> Span:
    """Start the root ``http.request`` span, honouring a remote (W3C) parent
    and the configured sampling ratio."""
    attrs = dict(attributes or {})
    attrs.setdefault("http.method", method)
    if _tracer is None:
        return INVALID_SPAN
    kwargs: dict[str, Any] = {"kind": otel_trace.SpanKind.SERVER}
    if remote_context is not None:
        kwargs["context"] = remote_context
    return _tracer.start_span("http.request", attributes=attrs, **kwargs)


def end_http_span(
    span: Span,
    *,
    status_code: int,
    duration_ms: float,
    path_template: str,
    error: bool,
    request_id: str = "",
) -> None:
    """Finalise a request span: attributes, error status and ``http.request``
    error escalation for always-on error recording (#24)."""
    if span is not None and span is not INVALID_SPAN:
        span.set_attribute("http.status_code", int(status_code))
        span.set_attribute("http.route", path_template)
        span.set_attribute("agentcms.duration_ms", round(duration_ms, 2))
        if request_id:
            span.set_attribute("agentcms.request_id", request_id)
        if error:
            span.set_attribute("error", True)
        if hasattr(span, "end"):
            span.end()

    # Guarantee an exported error trace even when the root was not sampled:
    # open an always-sampled error span under the same trace id.
    if error and _error_tracer is not None:
        method = ""
        if span is not None and hasattr(span, "attributes") and span.attributes:
            method = str(span.attributes.get("http.method", ""))
        attributes: dict[str, Any] = {
            "http.method": method,
            "http.status_code": int(status_code),
            "http.route": path_template,
            "error": True,
        }
        if request_id:
            attributes["agentcms.request_id"] = request_id
        error_span = _error_tracer.start_span(
            "http.request.error",
            kind=otel_trace.SpanKind.SERVER,
            attributes=attributes,
        )
        error_span.set_status(StatusCode.ERROR, f"http {status_code}")
        error_span.end()


def end_span(status: StatusCode | None = None) -> None:
    """End the most recently started span (safe to call with no open span)."""
    stack = getattr(_tls, "spans", None)
    if not stack:
        return
    span = stack.pop()
    if status is not None and hasattr(span, "set_status"):
        span.set_status(status)
    if hasattr(span, "end"):
        span.end()


def inject_traceparent(carrier: dict[str, str]) -> None:
    """Inject the current trace context into ``carrier`` (W3C traceparent)."""
    inject(carrier)


def extract_traceparent(carrier: dict[str, str]) -> Any:
    """Extract a remote trace context from headers (W3C traceparent)."""
    return extract(carrier)


def get_current_span() -> Span:
    return otel_trace.get_current_span()


def record_error(description: str = "unhandled error") -> None:
    """Mark the current span with an error status + event, if any."""
    span = otel_trace.get_current_span()
    if isinstance(span, Span) and span.is_recording():
        span.set_status(StatusCode.ERROR, description)
        span.record_exception(_ObsError(description))


class _ObsError(Exception):
    pass


def reset_for_test(exporter: Any = None) -> None:
    """Reset providers and (optionally) install an in-memory exporter.

    Test-only: lets tests assert on exported spans without an OTLP endpoint.
    """
    _reset()
    if exporter is not None:
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor

        _provider = TracerProvider(
            resource=Resource.create({"service.name": "test"}),
            sampler=_AgentSampler(1.0),
        )
        _provider.add_span_processor(SimpleSpanProcessor(exporter))
        global _tracer
        _tracer = _provider.get_tracer("agentcms")
        otel_trace.set_tracer_provider(_provider)


def get_exporter() -> Any:
    return _exporter
