"""Distributed tracing with OpenTelemetry (exported to Jaeger over OTLP/HTTP).

How one request becomes one trace:
  * every component creates spans for the work it does;
  * the W3C `traceparent` header carries the trace context on every HTTP call
    (instrumented httpx clients inject it; instrumented FastAPI apps and Kong extract it);
  * so gateway, coordinator, agents, services and database queries all appear in one tree.

Tracing is enabled only when OTEL_EXPORTER_OTLP_ENDPOINT is set (it is in docker-compose.yml).
Otherwise the OpenTelemetry API is a no-op: unit tests and local runs need no collector.

Correlation ID and trace ID are linked both ways:
  * each server span gets the attribute `correlation.id` (search for it in Jaeger: Tags);
  * every JSON log line gets `trace_id` (see observability.py).
"""
from __future__ import annotations

import importlib.util
import logging
import os
from contextlib import contextmanager
from typing import Any, Iterator

from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode

logger = logging.getLogger(__name__)
_instrumented_httpx = False


def tracer(name: str = "comco"):
    return trace.get_tracer(name)


def setup_tracing(app, service_name: str, *, database: bool = False) -> bool:
    """Instrument a FastAPI app (+ outgoing httpx calls, + psycopg if `database`)."""
    global _instrumented_httpx
    if not os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return False

    # Imported here so that code paths without tracing don't need the SDK installed.
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    if not isinstance(trace.get_tracer_provider(), TracerProvider):
        provider = TracerProvider(resource=Resource.create({
            "service.name": service_name,
            "service.namespace": "comco",
            "deployment.environment": os.getenv("DEPLOYMENT_ENV", "local"),
        }))
        # Exports in the background, in batches: a slow or missing Jaeger never slows requests.
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(provider)

    FastAPIInstrumentor.instrument_app(app, excluded_urls="health,ready,docs,openapi.json",
                                       exclude_spans=["receive", "send"])   # drop per-chunk ASGI noise
    if not _instrumented_httpx and importlib.util.find_spec("httpx"):   # agents call out with httpx
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        HTTPXClientInstrumentor().instrument(    # creates client spans + injects `traceparent`
            request_hook=_name_client_span, async_request_hook=_name_client_span_async)
        _instrumented_httpx = True
    if database:
        from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor
        PsycopgInstrumentor().instrument(enable_commenter=False, skip_dep_check=True)
    logger.info("tracing enabled", extra={"fields": {"otlp_endpoint": os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"]}})
    return True


def _name_client_span(span: Span, request) -> None:
    """'GET' -> 'GET gateway/api/orders' so outgoing calls are readable in Jaeger."""
    if span and span.is_recording():
        url = request.url if hasattr(request, "url") else request[1]
        method = request.method if hasattr(request, "method") else request[0]
        method = method.decode() if isinstance(method, bytes) else method
        span.update_name(f"{method} {url.host}{url.path}")


async def _name_client_span_async(span: Span, request) -> None:
    _name_client_span(span, request)


def current_trace_id() -> str | None:
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else None


def annotate(**attributes: Any) -> None:
    """Add attributes to the current span (no-op without tracing)."""
    span = trace.get_current_span()
    for key, value in attributes.items():
        if value is not None:
            span.set_attribute(key, value if isinstance(value, (str, bool, int, float)) else str(value))


def event(name: str, **attributes: Any) -> None:
    """Record a point-in-time event on the current span (e.g. a retry or a circuit change)."""
    trace.get_current_span().add_event(name, {k: str(v) for k, v in attributes.items() if v is not None})


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[Span]:
    """A child span for a meaningful unit of work, e.g. "plan" or "a2a.delegate get_order"."""
    with tracer().start_as_current_span(name) as s:
        annotate(**attributes)
        yield s


def mark_error(s: Span, code: str, message: str = "") -> None:
    s.set_status(Status(StatusCode.ERROR, f"{code}: {message}" if message else code))
    s.set_attribute("error.code", code)
