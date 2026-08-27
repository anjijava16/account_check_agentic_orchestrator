"""Observability introspection.

A process resource snapshot, the OTel/Jaeger/Prometheus wiring, and the list of
registered Prometheus metric names. The scrape endpoint itself stays at /metrics.
"""
from __future__ import annotations

import os
import shutil
from typing import Any

from fastapi import APIRouter, Depends

from app.api.deps import require_role
from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)
router = APIRouter(
    prefix="/services/observability",
    tags=["services: observability"],
    dependencies=[Depends(require_role("agent_operator"))],
)


@router.get("/resources", summary="Process resource snapshot")
async def resources() -> dict[str, Any]:
    snapshot: dict[str, Any] = {}
    try:
        import psutil  # type: ignore

        proc = psutil.Process(os.getpid())
        vm = psutil.virtual_memory()
        snapshot["cpu_percent"] = proc.cpu_percent(interval=0.1)
        snapshot["memory_rss_bytes"] = proc.memory_info().rss
        snapshot["system_memory_percent"] = vm.percent
        snapshot["num_threads"] = proc.num_threads()
    except ImportError:
        snapshot["note"] = "psutil not installed; disk metrics only"
    usage = shutil.disk_usage("/")
    snapshot["disk_used_percent"] = round(usage.used / usage.total * 100, 2)
    return snapshot


@router.get("/config", summary="Observability wiring")
async def config() -> dict[str, Any]:
    return {
        "otel_enabled": settings.otel_enabled,
        "otel_endpoint": settings.otel_endpoint,
        "otel_service_name": settings.otel_service_name,
        "jaeger_query_url": settings.jaeger_query_url,
        "metrics_path": settings.metrics_path,
        "cost_tracking_enabled": settings.cost_tracking_enabled,
    }


@router.get("/metrics-names", summary="Registered Prometheus metric names")
async def metric_names() -> dict[str, Any]:
    from prometheus_client import REGISTRY

    names = sorted(
        {
            getattr(metric, "name", "")
            for collector in list(REGISTRY._collector_to_names)  # noqa: SLF001
            for metric in collector.collect()
        }
    )
    names = [n for n in names if n]
    return {"count": len(names), "metrics": names}
