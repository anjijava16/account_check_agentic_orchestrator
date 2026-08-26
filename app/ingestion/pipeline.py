"""Ingestion pipeline: S3 -> parse -> chunk -> embed -> OpenSearch + Postgres.

Runs entirely in the background worker. Each stage is idempotent and records
an IngestionEvent, so a document can be replayed from any failure point
without duplicating chunks (deterministic chunk ids do the deduplication).

Stage order and why:
  1. fetch      -- read raw bytes back from S3 (never trust the request body)
  2. parse      -- layout-aware text extraction
  3. chunk      -- structure-aware splitting
  4. embed      -- batched, concurrency-limited calls through LiteLLM
  5. persist    -- chunk rows + pgvector copies in Postgres (the durable truth)
  6. index      -- bulk write into OpenSearch (the query engine)
  7. finalise   -- status + counters + processed-copy in S3
"""
from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.core.config import settings
from app.core.constants import DocumentStatus
from app.core.logging import get_logger
from app.db.repositories.documents import DocumentRepository
from app.db.session import session_scope
from app.ingestion.chunker import Chunk, StructureAwareChunker
from app.ingestion.parsers import ParsedDocument, get_parser_registry
from app.llm.cost_tracker import get_cost_tracker
from app.llm.gateway import get_gateway
from app.observability.metrics import (
    INGESTION_CHUNKS,
    INGESTION_DOCS,
    INGESTION_STAGE_LATENCY,
)
from app.observability.tracing import current_trace_id, span
from app.storage.s3 import get_object_store
from app.vector.hybrid_search import HybridSearcher

log = get_logger(__name__)


@dataclass(slots=True)
class IngestionResult:
    document_id: uuid.UUID
    status: str
    chunks: int = 0
    embedded: int = 0
    indexed: int = 0
    duration_ms: int = 0
    errors: list[str] = field(default_factory=list)


class IngestionPipeline:
    def __init__(self) -> None:
        self.store = get_object_store()
        self.parsers = get_parser_registry()
        self.chunker = StructureAwareChunker()
        self.gateway = get_gateway()
        self.searcher = HybridSearcher()
        self.cost = get_cost_tracker()

    async def run(self, document_id: uuid.UUID) -> IngestionResult:
        started = time.perf_counter()
        result = IngestionResult(document_id=document_id, status="failed")

        async with session_scope() as session:
            repo = DocumentRepository(session)
            doc = await repo.get(document_id)
            if doc is None:
                result.errors.append("document_not_found")
                return result
            meta = {
                "tenant_id": doc.tenant_id,
                "filename": doc.filename,
                "content_type": doc.content_type,
                "bucket": doc.s3_bucket,
                "key": doc.s3_key,
                "classification": doc.classification,
                "doc_metadata": dict(doc.doc_metadata or {}),
            }

        with span(
            "ingestion.run",
            **{"document.id": str(document_id), "document.tenant": meta["tenant_id"]},
        ):
            try:
                raw = await self._stage_fetch(document_id, meta)
                parsed = await self._stage_parse(document_id, raw, meta)
                chunks = await self._stage_chunk(document_id, parsed)
                if not chunks:
                    await self._fail(document_id, "no_extractable_content")
                    result.errors.append("no_extractable_content")
                    return result

                vectors = await self._stage_embed(document_id, chunks, meta)
                await self._stage_persist(document_id, chunks, vectors, meta)
                indexed = await self._stage_index(document_id, chunks, vectors, meta, parsed)
                await self._stage_finalise(document_id, parsed, len(chunks), indexed)

                result.status = DocumentStatus.COMPLETED
                result.chunks = len(chunks)
                result.embedded = len(vectors)
                result.indexed = indexed
                INGESTION_DOCS.labels(status="completed").inc()
                INGESTION_CHUNKS.labels(status="indexed").inc(indexed)
            except Exception as exc:  # noqa: BLE001
                log.exception("ingestion.failed", document_id=str(document_id))
                await self._fail(document_id, str(exc)[:2000])
                result.errors.append(str(exc))
                INGESTION_DOCS.labels(status="failed").inc()

        result.duration_ms = int((time.perf_counter() - started) * 1000)
        log.info(
            "ingestion.finished",
            document_id=str(document_id),
            status=result.status,
            chunks=result.chunks,
            indexed=result.indexed,
            duration_ms=result.duration_ms,
        )
        return result

    # ------------------------------------------------------------- stages
    async def _stage_fetch(self, document_id: uuid.UUID, meta: dict[str, Any]) -> bytes:
        async with self._stage(document_id, "fetch", DocumentStatus.PARSING):
            return await self.store.get(meta["key"], bucket=meta["bucket"])

    async def _stage_parse(
        self, document_id: uuid.UUID, raw: bytes, meta: dict[str, Any]
    ) -> ParsedDocument:
        async with self._stage(document_id, "parse", DocumentStatus.PARSING):
            return self.parsers.parse(raw, meta["filename"], meta["content_type"])

    async def _stage_chunk(
        self, document_id: uuid.UUID, parsed: ParsedDocument
    ) -> list[Chunk]:
        async with self._stage(document_id, "chunk", DocumentStatus.CHUNKING):
            return self.chunker.chunk(parsed)

    async def _stage_embed(
        self, document_id: uuid.UUID, chunks: list[Chunk], meta: dict[str, Any]
    ) -> list[list[float]]:
        async with self._stage(document_id, "embed", DocumentStatus.EMBEDDING):
            vectors, usages = await self.gateway.embed_batched(
                [c.content for c in chunks], batch_size=settings.embedding_batch_size
            )
            for usage in usages:
                await self.cost.track(
                    usage,
                    tenant_id=meta["tenant_id"],
                    call_type="embedding",
                    metadata={"document_id": str(document_id)},
                )
            return vectors

    async def _stage_persist(
        self,
        document_id: uuid.UUID,
        chunks: list[Chunk],
        vectors: list[list[float]],
        meta: dict[str, Any],
    ) -> None:
        async with self._stage(document_id, "persist", DocumentStatus.INDEXING):
            rows = [
                {
                    "id": uuid.uuid5(document_id, str(chunk.index)),
                    "document_id": document_id,
                    "tenant_id": meta["tenant_id"],
                    "chunk_index": chunk.index,
                    "content": chunk.content,
                    "content_sha256": chunk.sha256,
                    "token_count": chunk.token_count,
                    "page_number": chunk.page_number,
                    "section_path": chunk.section_path,
                    "heading": chunk.heading,
                    "embedding_model": settings.embedding_model,
                    "embedding": vector,
                    "opensearch_id": self._chunk_id(document_id, chunk.index),
                    "chunk_metadata": {"block_types": chunk.block_types},
                }
                for chunk, vector in zip(chunks, vectors, strict=False)
            ]
            async with session_scope() as session:
                await DocumentRepository(session).upsert_chunks(rows)

    async def _stage_index(
        self,
        document_id: uuid.UUID,
        chunks: list[Chunk],
        vectors: list[list[float]],
        meta: dict[str, Any],
        parsed: ParsedDocument,
    ) -> int:
        async with self._stage(document_id, "index", DocumentStatus.INDEXING):
            now = datetime.now(UTC).isoformat()
            payload = [
                {
                    "chunk_id": self._chunk_id(document_id, chunk.index),
                    "document_id": str(document_id),
                    "tenant_id": meta["tenant_id"],
                    "chunk_index": chunk.index,
                    "content": chunk.content,
                    "content_sha256": chunk.sha256,
                    "title": parsed.title or meta["filename"],
                    "heading": chunk.heading,
                    "section_path": chunk.section_path,
                    "filename": meta["filename"],
                    "page_number": chunk.page_number,
                    "token_count": chunk.token_count,
                    "classification": meta["classification"],
                    "doc_type": meta["doc_metadata"].get("doc_type", "general"),
                    "language": parsed.language,
                    "source_uri": f"s3://{meta['bucket']}/{meta['key']}",
                    "tags": meta["doc_metadata"].get("tags", []),
                    "effective_from": meta["doc_metadata"].get("effective_from"),
                    "effective_to": meta["doc_metadata"].get("effective_to"),
                    "created_at": now,
                    "embedding_model": settings.embedding_model,
                    "embedding": vector,
                }
                for chunk, vector in zip(chunks, vectors, strict=False)
            ]
            response = await self.searcher.bulk_index(payload, refresh=True)
            return int(response["indexed"])

    async def _stage_finalise(
        self, document_id: uuid.UUID, parsed: ParsedDocument, chunk_count: int, indexed: int
    ) -> None:
        async with session_scope() as session:
            await DocumentRepository(session).set_status(
                document_id,
                DocumentStatus.COMPLETED,
                chunk_count=chunk_count,
                embedded_count=chunk_count,
                indexed_count=indexed,
                page_count=parsed.page_count,
                parser_backend=parsed.backend,
                index_name=settings.opensearch_index_alias,
            )

    async def _fail(self, document_id: uuid.UUID, reason: str) -> None:
        async with session_scope() as session:
            repo = DocumentRepository(session)
            await repo.set_status(document_id, DocumentStatus.FAILED, failure_reason=reason)
            await repo.record_event(
                document_id, "pipeline", "failed", detail={"reason": reason},
                trace_id=current_trace_id(),
            )

    # -------------------------------------------------------------- helpers
    @staticmethod
    def _chunk_id(document_id: uuid.UUID, index: int) -> str:
        """Deterministic id => replays overwrite instead of duplicating."""
        return f"{document_id}:{index}"

    @asynccontextmanager
    async def _stage(self, document_id: uuid.UUID, name: str, status: DocumentStatus):
        """Wrap a stage: set status, time it, emit a span, record an event."""
        started = time.perf_counter()
        async with session_scope() as session:
            await DocumentRepository(session).set_status(document_id, status)
        error: Exception | None = None
        try:
            with span(f"ingestion.{name}", **{"document.id": str(document_id)}):
                yield
        except Exception as exc:  # noqa: BLE001
            error = exc
            raise
        finally:
            duration_ms = int((time.perf_counter() - started) * 1000)
            INGESTION_STAGE_LATENCY.labels(stage=name).observe(duration_ms / 1000)
            async with session_scope() as session:
                await DocumentRepository(session).record_event(
                    document_id,
                    name,
                    "failed" if error else "ok",
                    duration_ms=duration_ms,
                    detail={"error": str(error)[:1000]} if error else {},
                    trace_id=current_trace_id(),
                )
