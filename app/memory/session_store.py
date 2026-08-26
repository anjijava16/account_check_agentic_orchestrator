"""Redis-backed Session Store.

Two responsibilities from the architecture diagram:
  1. Conversation History  -- a capped, TTL'd list of turns per session.
  2. Inter-agent shared state -- a hash the coordinator and sub-agents both
     read/write within a turn (resolved account ids, retrieved context refs,
     pending approvals) so agents don't re-derive the same facts.
"""
from __future__ import annotations

import json
import time
from typing import Any

from app.core.config import settings
from app.core.logging import get_logger
from app.memory.redis_client import get_redis

log = get_logger(__name__)

HISTORY_KEY = "session:{sid}:history"
STATE_KEY = "session:{sid}:state"
META_KEY = "session:{sid}:meta"
LOCK_KEY = "session:{sid}:lock"
MAX_HISTORY_TURNS = 100


class SessionStore:
    def __init__(self, ttl: int | None = None) -> None:
        self.ttl = ttl or settings.session_ttl_seconds
        self.redis = get_redis()

    # ------------------------------------------------------- conversation
    async def append_turn(self, session_id: str, role: str, content: str, **meta: Any) -> None:
        entry = json.dumps(
            {"role": role, "content": content, "ts": time.time(), **meta}, default=str
        )
        key = HISTORY_KEY.format(sid=session_id)
        pipe = self.redis.pipeline()
        pipe.rpush(key, entry)
        pipe.ltrim(key, -MAX_HISTORY_TURNS, -1)
        pipe.expire(key, self.ttl)
        await pipe.execute()

    async def history(self, session_id: str, limit: int = 20) -> list[dict[str, Any]]:
        raw = await self.redis.lrange(HISTORY_KEY.format(sid=session_id), -limit, -1)
        out: list[dict[str, Any]] = []
        for item in raw:
            try:
                out.append(json.loads(item))
            except json.JSONDecodeError:
                continue
        return out

    async def clear_history(self, session_id: str) -> None:
        await self.redis.delete(HISTORY_KEY.format(sid=session_id))

    # --------------------------------------------------- inter-agent state
    async def set_shared(self, session_id: str, field: str, value: Any) -> None:
        key = STATE_KEY.format(sid=session_id)
        await self.redis.hset(key, field, json.dumps(value, default=str))
        await self.redis.expire(key, settings.shared_state_ttl_seconds)

    async def set_shared_many(self, session_id: str, mapping: dict[str, Any]) -> None:
        if not mapping:
            return
        key = STATE_KEY.format(sid=session_id)
        payload = {k: json.dumps(v, default=str) for k, v in mapping.items()}
        await self.redis.hset(key, mapping=payload)
        await self.redis.expire(key, settings.shared_state_ttl_seconds)

    async def get_shared(self, session_id: str, field: str) -> Any | None:
        raw = await self.redis.hget(STATE_KEY.format(sid=session_id), field)
        return json.loads(raw) if raw else None

    async def all_shared(self, session_id: str) -> dict[str, Any]:
        raw = await self.redis.hgetall(STATE_KEY.format(sid=session_id))
        out: dict[str, Any] = {}
        for k, v in raw.items():
            try:
                out[k] = json.loads(v)
            except json.JSONDecodeError:
                out[k] = v
        return out

    async def drop_shared(self, session_id: str) -> None:
        await self.redis.delete(STATE_KEY.format(sid=session_id))

    # ----------------------------------------------------------- metadata
    async def touch(self, session_id: str, **meta: Any) -> None:
        key = META_KEY.format(sid=session_id)
        payload = {k: json.dumps(v, default=str) for k, v in meta.items()}
        payload["last_seen"] = json.dumps(time.time())
        await self.redis.hset(key, mapping=payload)
        await self.redis.expire(key, self.ttl)

    # ------------------------------------------------------------- locking
    async def acquire_turn_lock(self, session_id: str, token: str, ttl: int = 120) -> bool:
        """One in-flight turn per session; prevents interleaved agent writes."""
        return bool(
            await self.redis.set(LOCK_KEY.format(sid=session_id), token, nx=True, ex=ttl)
        )

    async def release_turn_lock(self, session_id: str, token: str) -> None:
        script = """
        if redis.call('get', KEYS[1]) == ARGV[1] then
            return redis.call('del', KEYS[1])
        else
            return 0
        end
        """
        await self.redis.eval(script, 1, LOCK_KEY.format(sid=session_id), token)
