"""Idempotency-Key support for non-idempotent POST endpoints."""
from __future__ import annotations

import json
from typing import Any

from app.core.config import settings
from app.memory.redis_client import get_redis

KEY = "idem:{scope}:{key}"


class IdempotencyStore:
    def __init__(self) -> None:
        self.redis = get_redis()

    async def begin(self, scope: str, key: str) -> tuple[bool, dict[str, Any] | None]:
        """Returns (is_new, cached_response)."""
        redis_key = KEY.format(scope=scope, key=key)
        acquired = await self.redis.set(
            redis_key, json.dumps({"status": "in_progress"}), nx=True,
            ex=settings.idempotency_ttl_seconds,
        )
        if acquired:
            return True, None
        raw = await self.redis.get(redis_key)
        payload = json.loads(raw) if raw else None
        if payload and payload.get("status") == "completed":
            return False, payload.get("response")
        return False, None

    async def complete(self, scope: str, key: str, response: dict[str, Any]) -> None:
        await self.redis.set(
            KEY.format(scope=scope, key=key),
            json.dumps({"status": "completed", "response": response}, default=str),
            ex=settings.idempotency_ttl_seconds,
        )

    async def abort(self, scope: str, key: str) -> None:
        await self.redis.delete(KEY.format(scope=scope, key=key))
