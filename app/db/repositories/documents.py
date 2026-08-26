"""Repository for document + chunk metadata."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import DocumentStatus
from app.db.models import Document, DocumentChunk, IngestionEvent, OutboxEvent


class DocumentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, **fields: Any) -> Document:
        doc = Document(**fields)
        self.session.add(doc)
        await self.session.flush()
        return doc

    async def get(self, document_id: uuid.UUID, tenant_id: str | None = None) -> Document | None:
        stmt = select(Document).where(Document.id == document_id)
        if tenant_id:
            stmt = stmt.where(Document.tenant_id == tenant_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def find_by_hash(self, tenant_id: str, sha256: str) -> Document | None:
        stmt = select(Document).where(
            Document.tenant_id == tenant_id, Document.content_sha256 == sha256
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list(
        self,
        tenant_id: str,
        *,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Document], int]:
        base = select(Document).where(Document.tenant_id == tenant_id)
        if status:
            base = base.where(Document.status == status)
        total = (
            await self.session.execute(
                select(func.count()).select_from(base.subquery())
            )
        ).scalar_one()
        rows = (
            await self.session.execute(
                base.order_by(Document.created_at.desc()).limit(limit).offset(offset)
            )
        ).scalars().all()
        return list(rows), int(total)

    async def set_status(
        self,
        document_id: uuid.UUID,
        status: DocumentStatus | str,
        *,
        failure_reason: str | None = None,
        **extra: Any,
    ) -> None:
        values: dict[str, Any] = {"status": str(status), **extra}
        if failure_reason is not None:
            values["failure_reason"] = failure_reason
        if status == DocumentStatus.COMPLETED:
            values["ingested_at"] = datetime.now(UTC)
        await self.session.execute(
            update(Document).where(Document.id == document_id).values(**values)
        )

    async def increment_retry(self, document_id: uuid.UUID) -> int:
        stmt = (
            update(Document)
            .where(Document.id == document_id)
            .values(retry_count=Document.retry_count + 1)
            .returning(Document.retry_count)
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def record_event(
        self,
        document_id: uuid.UUID,
        stage: str,
        status: str,
        *,
        duration_ms: int | None = None,
        detail: dict | None = None,
        trace_id: str | None = None,
    ) -> None:
        self.session.add(
            IngestionEvent(
                document_id=document_id,
                stage=stage,
                status=status,
                duration_ms=duration_ms,
                detail=detail or {},
                trace_id=trace_id,
            )
        )

    async def upsert_chunks(self, rows: list[dict[str, Any]]) -> int:
        """Idempotent chunk write keyed on (document_id, chunk_index)."""
        if not rows:
            return 0
        stmt = pg_insert(DocumentChunk).values(rows)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_chunk_doc_index",
            set_={
                "content": stmt.excluded.content,
                "content_sha256": stmt.excluded.content_sha256,
                "token_count": stmt.excluded.token_count,
                "embedding": stmt.excluded.embedding,
                "embedding_model": stmt.excluded.embedding_model,
                "opensearch_id": stmt.excluded.opensearch_id,
                "chunk_metadata": stmt.excluded.chunk_metadata,
                "updated_at": func.now(),
            },
        )
        await self.session.execute(stmt)
        return len(rows)

    async def chunks_for(self, document_id: uuid.UUID) -> list[DocumentChunk]:
        stmt = (
            select(DocumentChunk)
            .where(DocumentChunk.document_id == document_id)
            .order_by(DocumentChunk.chunk_index)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def enqueue_outbox(self, event_type: str, document_id: uuid.UUID, payload: dict) -> None:
        self.session.add(
            OutboxEvent(
                aggregate_type="document",
                aggregate_id=str(document_id),
                event_type=event_type,
                payload=payload,
            )
        )
