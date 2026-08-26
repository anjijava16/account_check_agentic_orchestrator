"""Distributed sliding-window rate limiter + token-bucket burst control.

Implemented as a single Lua script so the check-and-increment is atomic across
all API replicas. Used by the edge middleware and by per-tenant LLM quotas.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.memory.redis_client import get_redis

SLIDING_WINDOW_LUA = """
local key      = KEYS[1]
local now_ms   = tonumber(ARGV[1])
local window   = tonumber(ARGV[2])
local limit    = tonumber(ARGV[3])
local member   = ARGV[4]

redis.call('ZREMRANGEBYSCORE', key, 0, now_ms - window)
local count = redis.call('ZCARD', key)
if count >= limit then
  local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
  local retry = 0
  if oldest[2] then retry = math.ceil((tonumber(oldest[2]) + window - now_ms) / 1000) end
  return {0, count, retry}
end
redis.call('ZADD', key, now_ms, member)
redis.call('PEXPIRE', key, window)
return {1, count + 1, 0}
"""


@dataclass(slots=True)
class RateLimitDecision:
    allowed: bool
    current: int
    limit: int
    retry_after_seconds: int


class RateLimiter:
    def __init__(self, namespace: str = "rl") -> None:
        self.redis = get_redis()
        self.namespace = namespace
        self._script = self.redis.register_script(SLIDING_WINDOW_LUA)

    async def check(
        self, identity: str, *, limit: int, window_seconds: int = 60, member: str | None = None
    ) -> RateLimitDecision:
        import time
        import uuid

        now_ms = int(time.time() * 1000)
        key = f"{self.namespace}:{identity}:{window_seconds}"
        allowed, current, retry = await self._script(
            keys=[key],
            args=[now_ms, window_seconds * 1000, limit, member or str(uuid.uuid4())],
        )
        return RateLimitDecision(bool(allowed), int(current), limit, int(retry))
