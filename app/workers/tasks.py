"""Background jobs.

The API never does heavy work inline: upload returns as soon as the bytes are
in S3 and the row is in Postgres. Everything after that runs here, with
retries, exponential backoff and a dead-letter status on the document row.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update

from app.core.config import settings
from app.core.constants import DocumentStatus
from app.core.logging import get_logger
from app.db.models import OutboxEvent
from app.db.repositories.documents import DocumentRepository
from app.db.session import session_scope
from app.ingestion.pipeline import IngestionPipeline
from app.observability.metrics import INGESTION_QUEUE_DEPTH
from app.workers.queue import JOB_INGEST_DOCUMENT

log = get_logger(__name__)


async def ingest_document(ctx: dict[str, Any], document_id: str) -> dict[str, Any]:
    """Main ingestion job. Idempotent: safe to replay on the same document."""
    doc_uuid = uuid.UUID(document_id)
    log.info("job.ingest_started", document_id=document_id, attempt=ctx.get("job_try", 1))

    pipeline: IngestionPipeline = ctx["pipeline"]
    result = await pipeline.run(doc_uuid)

    if result.status != DocumentStatus.COMPLETED:
        async with session_scope() as session:
            attempts = await DocumentRepository(session).increment_retry(doc_uuid)
        if attempts >= settings.ingestion_max_retries:
            async with session_scope() as session:
                await DocumentRepository(session).set_status(
                    doc_uuid,
                    DocumentStatus.QUARANTINED,
                    failure_reason="max_retries_exhausted",
                )
            log.error("job.ingest_quarantined", document_id=document_id, attempts=attempts)
            return {"status": "quarantined", "attempts": attempts}
        raise RuntimeError(f"Ingestion failed for {document_id}: {result.errors}")

    return {
        "status": result.status,
        "chunks": result.chunks,
        "indexed": result.indexed,
        "duration_ms": result.duration_ms,
    }


async def reindex_document(ctx: dict[str, Any], document_id: str, target_index: str) -> dict:
    """Rebuild OpenSearch documents from the pgvector copies -- no re-embedding,
    so a mapping change costs storage time only, not model spend."""
    from app.vector.hybrid_search import HybridSearcher
    from app.vector.pgvector_store import PgVectorStore

    doc_uuid = uuid.UUID(document_id)
    async with session_scope() as session:
        repo = DocumentRepository(session)
        doc = await repo.get(doc_uuid)
        if doc is None:
            return {"status": "not_found"}
        rows = await PgVectorStore(session).backfill_source(doc_uuid)
        chunks = await repo.chunks_for(doc_uuid)
        tenant, filename, classification = doc.tenant_id, doc.filename, doc.classification

    by_id = {str(c.id): c for c in chunks}
    payload = []
    for chunk_id, content, embedding in rows:
        chunk = by_id.get(chunk_id)
        if chunk is None or not embedding:
            continue
        payload.append(
            {
                "chunk_id": f"{document_id}:{chunk.chunk_index}",
                "document_id": document_id,
                "tenant_id": tenant,
                "chunk_index": chunk.chunk_index,
                "content": content,
                "content_sha256": chunk.content_sha256,
                "title": filename,
                "heading": chunk.heading,
                "section_path": chunk.section_path,
                "filename": filename,
                "page_number": chunk.page_number,
                "token_count": chunk.token_count,
                "classification": classification,
                "doc_type": "general",
                "language": "en",
                "source_uri": f"s3://{doc.s3_bucket}/{doc.s3_key}",
                "tags": [],
                "created_at": datetime.now(UTC).isoformat(),
                "embedding_model": chunk.embedding_model,
                "embedding": embedding,
            }
        )

    searcher = HybridSearcher(index=target_index)
    response = await searcher.bulk_index(payload, refresh=False)
    log.info("job.reindex_done", document_id=document_id, indexed=response["indexed"])
    return {"status": "ok", **response}


async def publish_outbox(ctx: dict[str, Any]) -> dict[str, Any]:
    """Drain the transactional outbox into the job queue.

    Guarantees at-least-once delivery: the DB write and the enqueue can never
    disagree because the enqueue is derived from a committed row.
    """
    redis = ctx["redis"]
    published = 0
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(OutboxEvent)
                .where(OutboxEvent.published_at.is_(None))
                .order_by(OutboxEvent.created_at)
                .limit(200)
                .with_for_update(skip_locked=True)
            )
        ).scalars().all()

        for event in rows:
            try:
                if event.event_type == "document.uploaded":
                    await redis.enqueue_job(
                        JOB_INGEST_DOCUMENT,
                        event.aggregate_id,
                        _queue_name=settings.ingestion_queue,
                    )
                await session.execute(
                    update(OutboxEvent)
                    .where(OutboxEvent.id == event.id)
                    .values(published_at=datetime.now(UTC))
                )
                published += 1
            except Exception as exc:  # noqa: BLE001, PERF203
                await session.execute(
                    update(OutboxEvent)
                    .where(OutboxEvent.id == event.id)
                    .values(attempts=OutboxEvent.attempts + 1, last_error=str(exc)[:1000])
                )
    if published:
        log.info("job.outbox_published", count=published)
    return {"published": published}


async def refresh_stats(ctx: dict[str, Any]) -> dict[str, Any]:
    """Keep queue-depth gauges honest for alerting."""
    redis = ctx["redis"]
    try:
        depth = await redis.zcard(f"arq:queue:{settings.ingestion_queue}")
        INGESTION_QUEUE_DEPTH.set(depth)
        return {"queue_depth": depth}
    except Exception:  # noqa: BLE001
        return {"queue_depth": -1}


async def run_online_eval(ctx: dict[str, Any], turn_payload: dict[str, Any]) -> dict[str, Any]:
    """Sampled online evaluation of a live turn (see app/evaluation)."""
    from app.evaluation.runner import evaluate_turn

    return await evaluate_turn(turn_payload)
