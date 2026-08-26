"""OpenTelemetry wiring + helpers for span-per-agent-step."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Status, StatusCode

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)
_tracer: trace.Tracer | None = None


def setup_tracing(app: Any | None = None) -> None:
    global _tracer
    if not settings.otel_enabled:
        _tracer = trace.get_tracer(__name__)
        return

    resource = Resource.create(
        {
            "service.name": settings.otel_service_name,
            "service.version": "1.0.0",
            "deployment.environment": settings.environment,
        }
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otel_endpoint, insecure=True))
    )
    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer(settings.otel_service_name)

    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.instrumentation.redis import RedisInstrumentor

        RedisInstrumentor().instrument()
        HTTPXClientInstrumentor().instrument()
        if app is not None:
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

            FastAPIInstrumentor.instrument_app(app, excluded_urls="/health,/metrics,/ready")
    except Exception as exc:  # noqa: BLE001
        log.warning("otel.autoinstrument_partial", error=str(exc))

    log.info("otel.ready", endpoint=settings.otel_endpoint)


def get_tracer() -> trace.Tracer:
    global _tracer
    if _tracer is None:
        _tracer = trace.get_tracer(settings.otel_service_name)
    return _tracer


@contextmanager
def span(name: str, **attributes: Any):
    """Span helper that records exceptions and flattens dict attributes."""
    with get_tracer().start_as_current_span(name) as sp:
        for key, value in attributes.items():
            if value is None:
                continue
            sp.set_attribute(key, value if isinstance(value, (str, int, float, bool)) else str(value))
        try:
            yield sp
        except Exception as exc:
            sp.record_exception(exc)
            sp.set_status(Status(StatusCode.ERROR, str(exc)))
            raise


def current_trace_id() -> str | None:
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else None


def current_span_id() -> str | None:
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.span_id, "016x") if ctx.is_valid else None
