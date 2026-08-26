"""Embedding-similarity cache for repeat questions.

Cheap wins: 'what is my balance' style questions arrive constantly. We cache
the *retrieval* result (not the personalised answer) keyed on the normalised
question embedding, so knowledge lookups skip a round trip to OpenSearch.
Never used for account-specific answers -- see `cacheable()`.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any

from app.core.config import settings
from app.core.constants import Intent
from app.memory.redis_client import get_redis

INDEX_KEY = "semcache:{tenant}:index"
ENTRY_KEY = "semcache:{tenant}:{digest}"
SIMILARITY_THRESHOLD = 0.94

NON_CACHEABLE = {
    Intent.BALANCE_ENQUIRY,
    Intent.TRANSACTION_DETAILS,
    Intent.STATEMENT_REQUEST,
    Intent.CHANGE_OF_ADDRESS,
    Intent.KYC_UPDATE,
    Intent.CHEQUE_BOOK_REQUEST,
}


def cacheable(intent: str) -> bool:
    return intent not in {i.value for i in NON_CACHEABLE}


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class SemanticCache:
    def __init__(self, tenant_id: str) -> None:
        self.tenant_id = tenant_id
        self.redis = get_redis()

    async def lookup(self, embedding: list[float]) -> dict[str, Any] | None:
        members = await self.redis.hgetall(INDEX_KEY.format(tenant=self.tenant_id))
        best_digest, best_score = None, 0.0
        for digest, vec_json in members.items():
            score = _cosine(embedding, json.loads(vec_json))
            if score > best_score:
                best_digest, best_score = digest, score
        if best_digest and best_score >= SIMILARITY_THRESHOLD:
            raw = await self.redis.get(
                ENTRY_KEY.format(tenant=self.tenant_id, digest=best_digest)
            )
            if raw:
                payload = json.loads(raw)
                payload["_cache"] = {"hit": True, "similarity": round(best_score, 4)}
                return payload
            await self.redis.hdel(INDEX_KEY.format(tenant=self.tenant_id), best_digest)
        return None

    async def store(self, question: str, embedding: list[float], payload: dict[str, Any]) -> None:
        digest = hashlib.sha256(question.strip().lower().encode()).hexdigest()[:32]
        pipe = self.redis.pipeline()
        pipe.set(
            ENTRY_KEY.format(tenant=self.tenant_id, digest=digest),
            json.dumps(payload, default=str),
            ex=settings.semantic_cache_ttl_seconds,
        )
        pipe.hset(INDEX_KEY.format(tenant=self.tenant_id), digest, json.dumps(embedding))
        pipe.expire(INDEX_KEY.format(tenant=self.tenant_id), settings.semantic_cache_ttl_seconds)
        await pipe.execute()
