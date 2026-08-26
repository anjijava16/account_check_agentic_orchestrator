"""Ingestion API contracts."""
from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class DocumentMetadataIn(BaseModel):
    doc_type: Literal["policy", "product", "faq", "terms", "procedure", "general"] = "general"
    classification: Literal["public", "internal", "confidential", "restricted"] = "internal"
    title: str | None = Field(default=None, max_length=512)
    tags: list[str] = Field(default_factory=list, max_length=20)
    effective_from: date | None = None
    effective_to: date | None = None
    source_system: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class UploadResponse(BaseModel):
    document_id: uuid.UUID
    status: str
    filename: str
    size_bytes: int
    content_sha256: str
    s3_uri: str
    duplicate_of: uuid.UUID | None = None
    message: str


class PresignRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=512)
    content_type: str = Field(default="application/octet-stream")
    size_bytes: int = Field(gt=0, le=5 * 1024 * 1024 * 1024)
    metadata: DocumentMetadataIn = Field(default_factory=DocumentMetadataIn)


class PresignResponse(BaseModel):
    document_id: uuid.UUID
    upload_url: str
    s3_key: str
    expires_in_seconds: int
    complete_url: str


class DocumentOut(BaseModel):
    document_id: uuid.UUID
    filename: str
    content_type: str
    size_bytes: int
    status: str
    classification: str
    chunk_count: int
    indexed_count: int
    page_count: int | None = None
    parser_backend: str | None = None
    failure_reason: str | None = None
    retry_count: int = 0
    created_at: datetime
    ingested_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class DocumentListResponse(BaseModel):
    total: int
    limit: int
    offset: int
    documents: list[DocumentOut]


class IngestionEventOut(BaseModel):
    stage: str
    status: str
    duration_ms: int | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class DocumentStatusResponse(BaseModel):
    document_id: uuid.UUID
    status: str
    chunk_count: int
    indexed_count: int
    failure_reason: str | None = None
    events: list[IngestionEventOut] = Field(default_factory=list)


class SearchRequestIn(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    top_k: int = Field(default=8, ge=1, le=50)
    strategy: Literal["hybrid", "bm25", "knn"] = "hybrid"
    rerank: bool = True
    compress: bool = True
    doc_type: str | None = None
    tags: list[str] = Field(default_factory=list)


class SearchHitOut(BaseModel):
    chunk_id: str
    document_id: str
    content: str
    score: float
    rerank_score: float | None = None
    filename: str | None = None
    page: int | None = None
    section: str | None = None
    bm25_rank: int | None = None
    knn_rank: int | None = None


class SearchResponse(BaseModel):
    query: str
    strategy: str
    returned: int
    took_ms: int
    hits: list[SearchHitOut]
