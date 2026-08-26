"""Health, readiness and liveness.

Three distinct signals, because Kubernetes needs them to mean different things:
  /live   -- the process is running (never touches a dependency)
  /ready  -- the pod can serve traffic (checks hard dependencies)
  /health -- full component report for humans and dashboards
"""
from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Response, status

from app.core.config import settings
from app.schemas.common import ComponentHealth, HealthResponse

router = APIRouter(tags=["health"])


async def _timed(name: str, coro) -> ComponentHealth:
    started = time.perf_counter()
    try:
        ok = await asyncio.wait_for(coro, timeout=3.0)
        return ComponentHealth(
            name=name,
            status="up" if ok else "down",
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
        )
    except Exception as exc:  # noqa: BLE001
        return ComponentHealth(
            name=name,
            status="down",
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
            detail=str(exc)[:200],
        )


async def _check_postgres() -> bool:
    from sqlalchemy import text

    from app.db.session import session_scope

    async with session_scope() as session:
        await session.execute(text("SELECT 1"))
    return True


async def _check_redis() -> bool:
    from app.memory.redis_client import ping

    return await ping()


async def _check_opensearch() -> bool:
    from app.vector.opensearch_client import ping

    return await ping()


async def _check_s3() -> bool:
    from app.storage.s3 import get_object_store

    await get_object_store().ensure_buckets()
    return True


@router.get("/live", summary="Liveness")
async def live() -> dict[str, str]:
    return {"status": "alive"}


@router.get("/ready", summary="Readiness")
async def ready(response: Response) -> dict[str, object]:
    checks = await asyncio.gather(
        _timed("postgres", _check_postgres()),
        _timed("redis", _check_redis()),
        _timed("opensearch", _check_opensearch()),
    )
    healthy = all(c.status == "up" for c in checks)
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "ready": healthy,
        "components": {c.name: c.status for c in checks},
    }


@router.get("/health", response_model=HealthResponse, summary="Full health report")
async def health() -> HealthResponse:
    from app.mcp.client.manager import get_mcp_manager

    components = list(
        await asyncio.gather(
            _timed("postgres", _check_postgres()),
            _timed("redis", _check_redis()),
            _timed("opensearch", _check_opensearch()),
            _timed("s3", _check_s3()),
        )
    )
    for server, ok in get_mcp_manager().health.items():
        components.append(
            ComponentHealth(name=f"mcp:{server}", status="up" if ok else "down")
        )

    down = sum(1 for c in components if c.status == "down")
    overall = "healthy" if down == 0 else ("degraded" if down <= 1 else "unhealthy")
    return HealthResponse(
        status=overall,
        version="1.0.0",
        environment=settings.environment,
        components=components,
    )
