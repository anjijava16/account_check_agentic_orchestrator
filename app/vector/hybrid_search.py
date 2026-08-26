"""Hybrid retrieval over OpenSearch.

Runs BM25 and kNN as two independent queries and fuses them with Reciprocal
Rank Fusion. RRF is preferred over score-weighted blending because BM25 and
cosine scores live on incomparable scales and drift as the corpus grows.

An optional cross-encoder rerank pass then reorders the fused top-N, and
contextual compression trims each chunk to the sentences that actually carry
the answer -- this is what keeps the prompt small and the citations honest.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from app.core.config import settings
from app.core.exceptions import VectorStoreError
from app.core.logging import get_logger
from app.observability.metrics import RETRIEVAL_LATENCY, RETRIEVAL_RESULTS
from app.observability.tracing import span
from app.vector.opensearch_client import get_client

log = get_logger(__name__)


@dataclass(slots=True)
class SearchHit:
    chunk_id: str
    document_id: str
    content: str
    score: float
    filename: str | None = None
    title: str | None = None
    heading: str | None = None
    page_number: int | None = None
    section_path: str | None = None
    source_uri: str | None = None
    classification: str = "internal"
    bm25_rank: int | None = None
    knn_rank: int | None = None
    rerank_score: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def citation(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "filename": self.filename,
            "page": self.page_number,
            "section": self.section_path or self.heading,
            "score": round(self.rerank_score if self.rerank_score is not None else self.score, 4),
        }


@dataclass(slots=True)
class SearchRequest:
    query: str
    embedding: list[float] | None = None
    tenant_id: str = "default"
    top_k: int = 8
    candidate_k: int = 50
    filters: dict[str, Any] = field(default_factory=dict)
    max_classification: str = "confidential"
    strategy: str = "hybrid"  # hybrid | bm25 | knn
    rerank: bool = True
    compress: bool = True


CLASSIFICATION_ORDER = ["public", "internal", "confidential", "restricted"]


def _filters(req: SearchRequest) -> list[dict[str, Any]]:
    clauses: list[dict[str, Any]] = [{"term": {"tenant_id": req.tenant_id}}]
    ceiling = CLASSIFICATION_ORDER.index(req.max_classification) if req.max_classification in CLASSIFICATION_ORDER else 2
    clauses.append(
        {"terms": {"classification": CLASSIFICATION_ORDER[: ceiling + 1]}}
    )
    for key, value in req.filters.items():
        clauses.append({"terms": {key: value}} if isinstance(value, list) else {"term": {key: value}})
    return clauses


def _source_fields() -> list[str]:
    return [
        "chunk_id", "document_id", "content", "filename", "title", "heading",
        "page_number", "section_path", "source_uri", "classification", "chunk_index",
        "doc_type", "tags",
    ]


class HybridSearcher:
    def __init__(self, index: str | None = None) -> None:
        self.index = index or settings.opensearch_index_alias
        self.client = get_client()

    # ------------------------------------------------------------- queries
    async def _bm25(self, req: SearchRequest) -> list[dict[str, Any]]:
        body = {
            "size": req.candidate_k,
            "_source": _source_fields(),
            "query": {
                "bool": {
                    "must": [
                        {
                            "multi_match": {
                                "query": req.query,
                                "fields": ["content^1.0", "title^2.0", "heading^1.5"],
                                "type": "best_fields",
                                "fuzziness": "AUTO",
                            }
                        }
                    ],
                    "filter": _filters(req),
                }
            },
        }
        response = await self.client.search(index=self.index, body=body)
        return response["hits"]["hits"]

    async def _knn(self, req: SearchRequest) -> list[dict[str, Any]]:
        if not req.embedding:
            return []
        body = {
            "size": req.candidate_k,
            "_source": _source_fields(),
            "query": {
                "bool": {
                    "must": [
                        {
                            "knn": {
                                "embedding": {
                                    "vector": req.embedding,
                                    "k": req.candidate_k,
                                }
                            }
                        }
                    ],
                    "filter": _filters(req),
                }
            },
        }
        response = await self.client.search(index=self.index, body=body)
        return response["hits"]["hits"]

    # ---------------------------------------------------------------- fusion
    def _rrf(
        self, bm25: list[dict[str, Any]], knn: list[dict[str, Any]]
    ) -> list[SearchHit]:
        k = settings.rrf_k
        scores: dict[str, float] = {}
        payloads: dict[str, dict[str, Any]] = {}
        ranks: dict[str, dict[str, int]] = {}

        for rank, hit in enumerate(bm25, start=1):
            doc_id = hit["_id"]
            scores[doc_id] = scores.get(doc_id, 0.0) + settings.hybrid_bm25_weight / (k + rank)
            payloads[doc_id] = hit["_source"]
            ranks.setdefault(doc_id, {})["bm25"] = rank

        for rank, hit in enumerate(knn, start=1):
            doc_id = hit["_id"]
            scores[doc_id] = scores.get(doc_id, 0.0) + settings.hybrid_knn_weight / (k + rank)
            payloads[doc_id] = hit["_source"]
            ranks.setdefault(doc_id, {})["knn"] = rank

        fused: list[SearchHit] = []
        for doc_id, score in sorted(scores.items(), key=lambda kv: kv[1], reverse=True):
            src = payloads[doc_id]
            fused.append(
                SearchHit(
                    chunk_id=src.get("chunk_id", doc_id),
                    document_id=src.get("document_id", ""),
                    content=src.get("content", ""),
                    score=round(score, 6),
                    filename=src.get("filename"),
                    title=src.get("title"),
                    heading=src.get("heading"),
                    page_number=src.get("page_number"),
                    section_path=src.get("section_path"),
                    source_uri=src.get("source_uri"),
                    classification=src.get("classification", "internal"),
                    bm25_rank=ranks.get(doc_id, {}).get("bm25"),
                    knn_rank=ranks.get(doc_id, {}).get("knn"),
                    metadata={"doc_type": src.get("doc_type"), "tags": src.get("tags", [])},
                )
            )
        return fused

    # ------------------------------------------------------------------ api
    async def search(self, req: SearchRequest) -> list[SearchHit]:
        started = time.perf_counter()
        with span(
            "retrieval.search",
            **{"retrieval.strategy": req.strategy, "retrieval.top_k": req.top_k},
        ):
            try:
                if req.strategy == "bm25":
                    bm25, knn = await self._bm25(req), []
                elif req.strategy == "knn":
                    bm25, knn = [], await self._knn(req)
                else:
                    bm25, knn = await asyncio.gather(self._bm25(req), self._knn(req))
            except Exception as exc:  # noqa: BLE001
                raise VectorStoreError(f"Search failed: {exc}") from exc

            hits = self._rrf(bm25, knn)

            if req.rerank and hits:
                from app.vector.reranker import rerank

                hits = await rerank(req.query, hits, top_k=req.top_k)
            else:
                hits = hits[: req.top_k]

            if req.compress and hits:
                from app.vector.compression import compress_hits

                hits = compress_hits(req.query, hits)

        elapsed = time.perf_counter() - started
        RETRIEVAL_LATENCY.labels(strategy=req.strategy).observe(elapsed)
        RETRIEVAL_RESULTS.labels(strategy=req.strategy).observe(len(hits))
        log.info(
            "retrieval.completed",
            strategy=req.strategy,
            bm25_candidates=len(bm25),
            knn_candidates=len(knn),
            returned=len(hits),
            duration_ms=int(elapsed * 1000),
        )
        return hits

    async def bulk_index(self, docs: list[dict[str, Any]], *, refresh: bool = False) -> dict[str, Any]:
        """Bulk upsert chunks. `_id` is the deterministic chunk id, so replays
        overwrite instead of duplicating."""
        if not docs:
            return {"indexed": 0, "errors": []}
        actions: list[dict[str, Any]] = []
        for doc in docs:
            actions.append({"index": {"_index": self.index, "_id": doc["chunk_id"]}})
            actions.append(doc)
        response = await self.client.bulk(body=actions, refresh=refresh)
        errors = [
            item["index"]
            for item in response.get("items", [])
            if item.get("index", {}).get("error")
        ]
        if errors:
            log.error("opensearch.bulk_errors", count=len(errors), sample=errors[:3])
        return {"indexed": len(docs) - len(errors), "errors": errors}

    async def delete_document(self, document_id: str, tenant_id: str) -> int:
        response = await self.client.delete_by_query(
            index=self.index,
            body={
                "query": {
                    "bool": {
                        "filter": [
                            {"term": {"document_id": document_id}},
                            {"term": {"tenant_id": tenant_id}},
                        ]
                    }
                }
            },
            refresh=True,
        )
        return int(response.get("deleted", 0))
