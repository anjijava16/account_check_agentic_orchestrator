"""Postgres CRUD over the `documents` table.

A concrete, tenant-scoped CRUD surface backed by the ORM repository -- all
queries are parameterized by SQLAlchemy, so there is no SQL-injection surface.
Writes here touch metadata only; the ingestion pipeline remains the path that
produces chunks and embeddings.
"""
from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.api.deps import PrincipalDep, require_role
from app.core.constants import DocumentStatus
from app.core.logging import get_logger
from app.db.repositories.documents import DocumentRepository
from app.db.session import session_scope

log = get_logger(__name__)
router = APIRouter(
    prefix="/services/postgres",
    tags=["services: postgres"],
    dependencies=[Depends(require_role("agent_operator"))],
)


class DocumentCreate(BaseModel):
    filename: str = Field(min_length=1, max_length=512)
    content_type: str = Field(default="application/octet-stream", max_length=128)
    size_bytes: int = Field(ge=0)
    content_sha256: str = Field(min_length=8, max_length=64)
    s3_bucket: str = Field(min_length=1, max_length=255)
    s3_key: str = Field(min_length=1, max_length=1024)
    classification: str = Field(default="internal", max_length=32)
    doc_metadata: dict[str, Any] = Field(default_factory=dict)


class DocumentUpdate(BaseModel):
    status: DocumentStatus | None = None
    classification: str | None = Field(default=None, max_length=32)
    doc_metadata: dict[str, Any] | None = None
    failure_reason: str | None = Field(default=None, max_length=2000)


def _serialize(doc: Any) -> dict[str, Any]:
    return {
        "id": str(doc.id),
        "tenant_id": doc.tenant_id,
        "filename": doc.filename,
        "content_type": doc.content_type,
        "size_bytes": doc.size_bytes,
        "content_sha256": doc.content_sha256,
        "s3_bucket": doc.s3_bucket,
        "s3_key": doc.s3_key,
        "status": doc.status,
        "classification": doc.classification,
        "chunk_count": doc.chunk_count,
        "indexed_count": doc.indexed_count,
        "doc_metadata": doc.doc_metadata,
        "created_at": doc.created_at.isoformat() if doc.created_at else None,
    }


def _parse_uuid(document_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(document_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid document id") from exc


@router.post("/documents", status_code=status.HTTP_201_CREATED, summary="Create a document row")
async def create_document(body: DocumentCreate, principal: PrincipalDep) -> dict[str, Any]:
    async with session_scope() as session:
        repo = DocumentRepository(session)
        if await repo.find_by_hash(principal.tenant_id, body.content_sha256):
            raise HTTPException(status_code=409, detail="Document with this hash already exists")
        doc = await repo.create(
            tenant_id=principal.tenant_id,
            uploaded_by=principal.subject,
            status=DocumentStatus.RECEIVED,
            **body.model_dump(),
        )
        await session.flush()
        return _serialize(doc)


@router.get("/documents", summary="List documents")
async def list_documents(
    principal: PrincipalDep,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    async with session_scope() as session:
        repo = DocumentRepository(session)
        rows, total = await repo.list(
            principal.tenant_id, status=status_filter, limit=limit, offset=offset
        )
        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "documents": [_serialize(d) for d in rows],
        }


@router.get("/documents/{document_id}", summary="Read a document")
async def read_document(document_id: str, principal: PrincipalDep) -> dict[str, Any]:
    doc_id = _parse_uuid(document_id)
    async with session_scope() as session:
        doc = await DocumentRepository(session).get(doc_id, tenant_id=principal.tenant_id)
        if doc is None:
            raise HTTPException(status_code=404, detail="Document not found")
        return _serialize(doc)


@router.patch("/documents/{document_id}", summary="Update a document")
async def update_document(
    document_id: str, body: DocumentUpdate, principal: PrincipalDep
) -> dict[str, Any]:
    doc_id = _parse_uuid(document_id)
    async with session_scope() as session:
        repo = DocumentRepository(session)
        doc = await repo.get(doc_id, tenant_id=principal.tenant_id)
        if doc is None:
            raise HTTPException(status_code=404, detail="Document not found")
        extra: dict[str, Any] = {}
        if body.classification is not None:
            extra["classification"] = body.classification
        if body.doc_metadata is not None:
            extra["doc_metadata"] = body.doc_metadata
        new_status = body.status or doc.status
        await repo.set_status(
            doc_id, new_status, failure_reason=body.failure_reason, **extra
        )
        refreshed = await repo.get(doc_id, tenant_id=principal.tenant_id)
        return _serialize(refreshed)


@router.delete("/documents/{document_id}", summary="Delete a document")
async def delete_document(document_id: str, principal: PrincipalDep) -> dict[str, Any]:
    doc_id = _parse_uuid(document_id)
    async with session_scope() as session:
        deleted = await DocumentRepository(session).delete(
            doc_id, tenant_id=principal.tenant_id
        )
        if not deleted:
            raise HTTPException(status_code=404, detail="Document not found")
        return {"id": document_id, "deleted": True}
