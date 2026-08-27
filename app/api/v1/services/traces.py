"""Distributed traces via the Jaeger query API.

Traces are immutable telemetry: Jaeger exposes read + create-by-emitting, but no
update or delete. So this router implements Read (services, traces, trace-by-id),
a Create that emits a real span, and returns 501 for update/delete by design.
"""
from __future__ import annotations

import re
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.deps import require_role
from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)
router = APIRouter(
    prefix="/services/traces",
    tags=["services: traces (jaeger)"],
    dependencies=[Depends(require_role("agent_operator"))],
)

_TRACE_ID_RE = re.compile(r"^[0-9a-fA-F]{1,32}$")


async def _jaeger_get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    url = f"{settings.jaeger_query_url.rstrip('/')}{path}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=exc.response.status_code, detail=f"Jaeger error: {exc}"
        ) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Jaeger unreachable: {exc}") from exc


@router.get("/services", summary="List services reporting traces")
async def list_services() -> dict[str, Any]:
    data = await _jaeger_get("/api/services")
    services = data.get("data") or []
    return {"count": len(services), "services": services}


@router.get("", summary="Query traces for a service")
async def list_traces(
    service: Annotated[str | None, Query(max_length=128)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
    lookback: Annotated[str, Query(max_length=16)] = "1h",
) -> dict[str, Any]:
    svc = service or settings.otel_service_name
    data = await _jaeger_get(
        "/api/traces", params={"service": svc, "limit": limit, "lookback": lookback}
    )
    traces = data.get("data") or []
    return {
        "service": svc,
        "count": len(traces),
        "traces": [
            {
                "trace_id": t.get("traceID"),
                "spans": len(t.get("spans", [])),
                "root": next(
                    (s.get("operationName") for s in t.get("spans", []) if not s.get("references")),
                    None,
                ),
            }
            for t in traces
        ],
    }


@router.get("/{trace_id}", summary="Read a trace by id")
async def read_trace(trace_id: str) -> dict[str, Any]:
    if not _TRACE_ID_RE.match(trace_id):
        raise HTTPException(status_code=400, detail="Invalid trace id")
    data = await _jaeger_get(f"/api/traces/{trace_id}")
    traces = data.get("data") or []
    if not traces:
        raise HTTPException(status_code=404, detail=f"Trace not found: {trace_id}")
    return traces[0]


@router.post("/_test-span", status_code=status.HTTP_201_CREATED, summary="Emit a test span")
async def emit_test_span(name: Annotated[str, Query(max_length=128)] = "manual-test-span") -> dict[str, Any]:
    """Create a trace by emitting a span through the configured OTel exporter."""
    from opentelemetry import trace

    tracer = trace.get_tracer(settings.otel_service_name)
    with tracer.start_as_current_span(name) as span:
        ctx = span.get_span_context()
        trace_id = format(ctx.trace_id, "032x") if ctx and ctx.trace_id else None
    return {
        "emitted": True,
        "trace_id": trace_id,
        "note": "Traces flush asynchronously; allow a moment before querying Jaeger.",
    }


@router.put("/{trace_id}", include_in_schema=False)
@router.delete("/{trace_id}", include_in_schema=False)
async def unsupported(trace_id: str) -> None:
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Traces are immutable; Jaeger supports no update or delete.",
    )
