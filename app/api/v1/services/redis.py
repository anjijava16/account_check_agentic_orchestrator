"""Redis key/value CRUD.

Thin operator surface over the shared Redis connection. redis-py sends commands
over RESP with arguments passed positionally, so user-supplied keys/values are
never interpolated into a command string (no injection surface).
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.api.deps import require_role
from app.core.logging import get_logger
from app.memory.redis_client import get_redis

log = get_logger(__name__)
router = APIRouter(
    prefix="/services/redis",
    tags=["services: redis"],
    dependencies=[Depends(require_role("agent_operator"))],
)

_MAX_KEY_LEN = 512
_MAX_SCAN = 1000


class RedisKeyCreate(BaseModel):
    key: str = Field(min_length=1, max_length=_MAX_KEY_LEN)
    value: str = Field(max_length=1_048_576)
    ttl_seconds: int | None = Field(default=None, ge=1, le=60 * 60 * 24 * 30)


class RedisValueUpdate(BaseModel):
    value: str = Field(max_length=1_048_576)
    ttl_seconds: int | None = Field(default=None, ge=1, le=60 * 60 * 24 * 30)


@router.post("/keys", status_code=status.HTTP_201_CREATED, summary="Create a key")
async def create_key(body: RedisKeyCreate) -> dict[str, Any]:
    redis = get_redis()
    if await redis.exists(body.key):
        raise HTTPException(status_code=409, detail=f"Key already exists: {body.key}")
    await redis.set(body.key, body.value, ex=body.ttl_seconds)
    return {"key": body.key, "ttl_seconds": body.ttl_seconds, "created": True}


@router.get("/keys", summary="List keys by pattern")
async def list_keys(
    pattern: Annotated[str, Query(max_length=_MAX_KEY_LEN)] = "*",
    limit: Annotated[int, Query(ge=1, le=_MAX_SCAN)] = 100,
) -> dict[str, Any]:
    redis = get_redis()
    keys: list[str] = []
    cursor = 0
    while len(keys) < limit:
        cursor, batch = await redis.scan(cursor=cursor, match=pattern, count=100)
        keys.extend(batch)
        if cursor == 0:
            break
    return {"pattern": pattern, "count": len(keys[:limit]), "keys": keys[:limit]}


@router.get("/keys/{key}", summary="Read a key")
async def read_key(key: str) -> dict[str, Any]:
    redis = get_redis()
    value = await redis.get(key)
    if value is None:
        raise HTTPException(status_code=404, detail=f"Key not found: {key}")
    ttl = await redis.ttl(key)
    return {"key": key, "value": value, "ttl_seconds": ttl if ttl >= 0 else None}


@router.put("/keys/{key}", summary="Update a key")
async def update_key(key: str, body: RedisValueUpdate) -> dict[str, Any]:
    redis = get_redis()
    if not await redis.exists(key):
        raise HTTPException(status_code=404, detail=f"Key not found: {key}")
    await redis.set(key, body.value, ex=body.ttl_seconds)
    return {"key": key, "ttl_seconds": body.ttl_seconds, "updated": True}


@router.delete("/keys/{key}", summary="Delete a key")
async def delete_key(key: str) -> dict[str, Any]:
    redis = get_redis()
    removed = await redis.delete(key)
    if not removed:
        raise HTTPException(status_code=404, detail=f"Key not found: {key}")
    return {"key": key, "deleted": True}


@router.get("/_info", summary="Server info")
async def server_info() -> dict[str, Any]:
    redis = get_redis()
    info = await redis.info()
    return {
        "db_size": await redis.dbsize(),
        "version": info.get("redis_version"),
        "used_memory_human": info.get("used_memory_human"),
        "connected_clients": info.get("connected_clients"),
        "uptime_seconds": info.get("uptime_in_seconds"),
    }
