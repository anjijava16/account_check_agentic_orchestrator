"""Shared async Redis connection pool."""
from __future__ import annotations

from redis.asyncio import ConnectionPool, Redis

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

_pool: ConnectionPool | None = None
_client: Redis | None = None


def get_redis() -> Redis:
    global _pool, _client
    if _client is None:
        _pool = ConnectionPool.from_url(
            str(settings.redis_dsn),
            max_connections=settings.redis_max_connections,
            decode_responses=True,
            health_check_interval=30,
        )
        _client = Redis(connection_pool=_pool)
    return _client


async def close_redis() -> None:
    global _pool, _client
    if _client is not None:
        await _client.aclose()
        _client = None
    if _pool is not None:
        await _pool.disconnect()
        _pool = None
    log.info("redis.closed")


async def ping() -> bool:
    try:
        return bool(await get_redis().ping())
    except Exception as exc:  # noqa: BLE001
        log.warning("redis.ping_failed", error=str(exc))
        return False
