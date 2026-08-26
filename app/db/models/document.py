"""Document / chunk metadata. OpenSearch owns the vectors, Postgres owns the truth."""
from __future__ import annotations

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.config import settings
from app.core.constants import DocumentStatus
from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Document(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("tenant_id", "content_sha256", name="uq_documents_tenant_sha"),
        Index("ix_documents_status_created", "status", "created_at"),
        Index("ix_documents_metadata_gin", "doc_metadata", postgresql_using="gin"),
        {"schema": "banking"},
    )

    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    uploaded_by: Mapped[str] = mapped_column(String(128), nullable=False)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # Object storage coordinates -- the raw bytes never live in Postgres.
    s3_bucket: Mapped[str] = mapped_column(String(255), nullable=False)
    s3_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    s3_version_id: Mapped[str | None] = mapped_column(String(255))
    processed_s3_key: Mapped[str | None] = mapped_column(String(1024))

    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=DocumentStatus.RECEIVED, index=True
    )
    failure_reason: Mapped[str | None] = mapped_column(Text)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    parser_backend: Mapped[str | None] = mapped_column(String(32))
    page_count: Mapped[int | None] = mapped_column(Integer)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    embedded_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    indexed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    index_name: Mapped[str | None] = mapped_column(String(255))
    classification: Mapped[str] = mapped_column(String(32), nullable=False, default="internal")
    doc_metadata: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    ingested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    chunks: Mapped[list[DocumentChunk]] = relationship(
        back_populates="document", cascade="all, delete-orphan", lazy="selectin"
    )
    events: Mapped[list[IngestionEvent]] = relationship(
        back_populates="document", cascade="all, delete-orphan", lazy="noload"
    )


class DocumentChunk(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Chunk metadata + a pgvector copy of the embedding.

    OpenSearch is the primary ANN engine. The pgvector column exists for
    (a) exact-recall reindex/backfill without re-embedding, and
    (b) small-corpus semantic queries that join relational filters.
    """

    __tablename__ = "document_chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "chunk_index", name="uq_chunk_doc_index"),
        Index("ix_chunks_tenant", "tenant_id"),
        Index(
            "ix_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        CheckConstraint("token_count > 0", name="token_count_positive"),
        {"schema": "banking"},
    )

    document_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("banking.documents.id", ondelete="CASCADE"), index=True
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)

    page_number: Mapped[int | None] = mapped_column(Integer)
    section_path: Mapped[str | None] = mapped_column(String(1024))
    heading: Mapped[str | None] = mapped_column(String(512))

    embedding_model: Mapped[str | None] = mapped_column(String(128))
    embedding: Mapped[list[float] | None] = mapped_column(Vector(settings.embedding_dimensions))

    opensearch_id: Mapped[str | None] = mapped_column(String(128), index=True)
    chunk_metadata: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    document: Mapped[Document] = relationship(back_populates="chunks")


class IngestionEvent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Append-only audit trail of every state transition in the pipeline."""

    __tablename__ = "ingestion_events"
    __table_args__ = ({"schema": "banking"},)

    document_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("banking.documents.id", ondelete="CASCADE"), index=True
    )
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    trace_id: Mapped[str | None] = mapped_column(String(64), index=True)

    document: Mapped[Document] = relationship(back_populates="events")
