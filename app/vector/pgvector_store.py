"""pgvector store.

Complements OpenSearch rather than competing with it:
  * exact cosine search when the filter is highly relational
    ("chunks from documents uploaded by this team, effective this quarter")
  * a durable copy of every embedding, so a mapping change or an index rebuild
    never requires paying the embedding cost again
  * dedupe by nearest-neighbour before indexing near-identical chunks
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models import Document, DocumentChunk

log = get_logger(__name__)


class PgVectorStore:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def similarity_search(
        self,
        embedding: list[float],
        *,
        tenant_id: str,
        top_k: int = 10,
        document_ids: list[uuid.UUID] | None = None,
        min_similarity: float = 0.0,
    ) -> list[dict[str, Any]]:
        distance = DocumentChunk.embedding.cosine_distance(embedding).label("distance")
        stmt = (
            select(
                DocumentChunk.id,
                DocumentChunk.document_id,
                DocumentChunk.content,
                DocumentChunk.chunk_index,
                DocumentChunk.page_number,
                DocumentChunk.section_path,
                Document.filename,
                distance,
            )
            .join(Document, Document.id == DocumentChunk.document_id)
            .where(DocumentChunk.tenant_id == tenant_id, DocumentChunk.embedding.isnot(None))
            .order_by(distance)
            .limit(top_k)
        )
        if document_ids:
            stmt = stmt.where(DocumentChunk.document_id.in_(document_ids))

        rows = (await self.session.execute(stmt)).all()
        results: list[dict[str, Any]] = []
        for row in rows:
            similarity = 1.0 - float(row.distance)
            if similarity < min_similarity:
                continue
            results.append(
                {
                    "chunk_id": str(row.id),
                    "document_id": str(row.document_id),
                    "content": row.content,
                    "chunk_index": row.chunk_index,
                    "page_number": row.page_number,
                    "section_path": row.section_path,
                    "filename": row.filename,
                    "similarity": round(similarity, 4),
                }
            )
        return results

    async def find_near_duplicate(
        self, embedding: list[float], *, tenant_id: str, threshold: float = 0.98
    ) -> dict[str, Any] | None:
        matches = await self.similarity_search(
            embedding, tenant_id=tenant_id, top_k=1, min_similarity=threshold
        )
        return matches[0] if matches else None

    async def backfill_source(
        self, document_id: uuid.UUID
    ) -> list[tuple[str, str, list[float]]]:
        """Stream (chunk_id, content, embedding) for re-indexing into a new
        OpenSearch index without recomputing embeddings."""
        stmt = (
            select(DocumentChunk.id, DocumentChunk.content, DocumentChunk.embedding)
            .where(DocumentChunk.document_id == document_id)
            .order_by(DocumentChunk.chunk_index)
        )
        return [
            (str(r.id), r.content, list(r.embedding) if r.embedding is not None else [])
            for r in (await self.session.execute(stmt)).all()
        ]

    async def ensure_extension(self) -> None:
        await self.session.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
