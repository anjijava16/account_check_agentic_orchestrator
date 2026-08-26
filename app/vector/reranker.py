"""Cross-encoder reranking.

The fused candidate list is ordered by rank position, not by relevance to the
exact question. A cross-encoder scores (query, chunk) jointly and typically
moves nDCG@5 up 10-20 points on banking policy corpora. Falls back to a
lexical-overlap heuristic when the reranker endpoint is unavailable so search
degrades rather than fails.
"""
from __future__ import annotations

import re
from collections import Counter

import httpx

from app.core.config import settings
from app.core.logging import get_logger
from app.vector.hybrid_search import SearchHit

log = get_logger(__name__)

RERANK_MODEL = "self-hosted-bge-reranker"
_TOKEN = re.compile(r"[a-z0-9]+")


def _lexical_score(query: str, text: str) -> float:
    q = Counter(_TOKEN.findall(query.lower()))
    d = Counter(_TOKEN.findall(text.lower()))
    if not q or not d:
        return 0.0
    overlap = sum((q & d).values())
    return overlap / (sum(q.values()) ** 0.5 * sum(d.values()) ** 0.5)


async def rerank(query: str, hits: list[SearchHit], *, top_k: int = 8) -> list[SearchHit]:
    if not hits:
        return []
    try:
        scores = await _remote_rerank(query, [h.content for h in hits])
    except Exception as exc:  # noqa: BLE001
        log.warning("rerank.fallback_lexical", error=str(exc))
        scores = [_lexical_score(query, h.content) for h in hits]

    for hit, score in zip(hits, scores, strict=False):
        hit.rerank_score = float(score)

    ordered = sorted(hits, key=lambda h: h.rerank_score or 0.0, reverse=True)
    return ordered[:top_k]


async def _remote_rerank(query: str, documents: list[str]) -> list[float]:
    from app.llm.model_registry import MODEL_REGISTRY

    spec = MODEL_REGISTRY[RERANK_MODEL]
    base = spec.api_base or settings.litellm_base_url
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            f"{base.rstrip('/')}/rerank",
            json={"query": query, "documents": documents, "model": spec.litellm_model},
            headers={"Authorization": f"Bearer {settings.litellm_master_key}"},
        )
        response.raise_for_status()
        payload = response.json()

    results = payload.get("results") or payload.get("data") or []
    scores = [0.0] * len(documents)
    for item in results:
        idx = item.get("index")
        if idx is not None and 0 <= idx < len(scores):
            scores[idx] = float(item.get("relevance_score", item.get("score", 0.0)))
    return scores
