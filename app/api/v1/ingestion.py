"""IngestionRouter.

Handles everything on the write path of the knowledge base.

The critical design decision: **the upload endpoint does not parse, chunk, or
embed anything.** It writes bytes to S3, writes one row to Postgres, writes one
outbox event, and returns. Median latency is a few hundred milliseconds
regardless of whether the file is 10 KB or 200 MB. All real work happens in the
arq worker, which can be scaled, retried and rate-limited independently of the
API pods.

Endpoints:
  POST   /ingestion/documents              -- direct upload (small files)
  POST   /ingestion/documents/presign      -- presigned URL (large files)
  POST   /ingestion/documents/{id}/complete-- notify after a presigned upload
  GET    /ingestion/documents              -- list with status
  GET    /ingestion/documents/{id}         -- one document
  GET    /ingestion/documents/{id}/status  -- pipeline stage history
  POST   /ingestion/documents/{id}/reindex -- rebuild from stored embeddings
  DELETE /ingestion/documents/{id}         -- purge from index, S3 and Postgres
  POST   /ingestion/search                 -- hybrid search (debug/eval surface)
"""
from __future__ import annotations

import time
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile, status

from app.api.deps import PrincipalDep, require_scope
from app.core.config import settings
from app.core.constants import DocumentStatus
from app.core.exceptions import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.db.repositories.documents import DocumentRepository
from app.db.session import session_scope
from app.schemas.common import AcceptedResponse
from app.schemas.ingestion import (
    DocumentListResponse,
    DocumentMetadataIn,
    DocumentOut,
    DocumentStatusResponse,
    IngestionEventOut,
    PresignRequest,
    PresignResponse,
    SearchHitOut,
    SearchRequestIn,
    SearchResponse,
    UploadResponse,
)
from app.storage.s3 import get_object_store, sha256_of

log = get_logger(__name__)
router = APIRouter(prefix="/ingestion", tags=["ingestion"])

ALLOWED_CONTENT_TYPES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "text/plain",
    "text/markdown",
    "text/html",
    "text/csv",
    "application/json",
    "image/png",
    "image/jpeg",
}


def _to_out(doc) -> DocumentOut:
    return DocumentOut(
        document_id=doc.id,
        filename=doc.filename,
        content_type=doc.content_type,
        size_bytes=doc.size_bytes,
        status=doc.status,
        classification=doc.classification,
        chunk_count=doc.chunk_count,
        indexed_count=doc.indexed_count,
        page_count=doc.page_count,
        parser_backend=doc.parser_backend,
        failure_reason=doc.failure_reason,
        retry_count=doc.retry_count,
        created_at=doc.created_at,
        ingested_at=doc.ingested_at,
        metadata=doc.doc_metadata,
    )


@router.post(
    "/documents",
    response_model=UploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload a document",
    dependencies=[Depends(require_scope("documents:write"))],
)
async def upload_document(
    principal: PrincipalDep,
    file: Annotated[UploadFile, File(description="The document to ingest")],
    doc_type: Annotated[str, Form()] = "general",
    classification: Annotated[str, Form()] = "internal",
    tags: Annotated[str, Form(description="Comma-separated")] = "",
) -> UploadResponse:
    """Accept a document, store it, and queue it for background processing.

    Content-addressed: re-uploading identical bytes returns the existing
    document instead of creating a duplicate and paying to embed it twice.
    """
    if file.content_type not in ALLOWED_CONTENT_TYPES:
        raise ValidationError(
            f"Unsupported content type: {file.content_type}",
            details={"allowed": sorted(ALLOWED_CONTENT_TYPES)},
        )

    data = await file.read()
    if not data:
        raise ValidationError("Uploaded file is empty")
    if len(data) > settings.max_body_bytes:
        raise ValidationError(
            "File exceeds the direct-upload limit; use the presign endpoint",
            details={"max_bytes": settings.max_body_bytes},
        )

    digest = sha256_of(data)

    async with session_scope() as session:
        repo = DocumentRepository(session)
        existing = await repo.find_by_hash(principal.tenant_id, digest)
        if existing is not None:
            log.info("ingestion.duplicate_upload", document_id=str(existing.id))
            return UploadResponse(
                document_id=existing.id,
                status=existing.status,
                filename=existing.filename,
                size_bytes=existing.size_bytes,
                content_sha256=digest,
                s3_uri=f"s3://{existing.s3_bucket}/{existing.s3_key}",
                duplicate_of=existing.id,
                message="Identical content already ingested; returning the existing document.",
            )

    document_id = uuid.uuid4()
    key = get_object_store().build_key(principal.tenant_id, str(document_id), file.filename or "upload")
    stored = await get_object_store().put(
        key=key,
        data=data,
        content_type=file.content_type,
        metadata={
            "document_id": str(document_id),
            "tenant_id": principal.tenant_id,
            "uploaded_by": principal.subject,
        },
    )

    async with session_scope() as session:
        repo = DocumentRepository(session)
        await repo.create(
            id=document_id,
            tenant_id=principal.tenant_id,
            uploaded_by=principal.subject,
            filename=file.filename or "upload",
            content_type=file.content_type,
            size_bytes=len(data),
            content_sha256=digest,
            s3_bucket=stored["bucket"],
            s3_key=stored["key"],
            s3_version_id=stored.get("version_id"),
            status=DocumentStatus.QUEUED,
            classification=classification,
            doc_metadata={
                "doc_type": doc_type,
                "tags": [t.strip() for t in tags.split(",") if t.strip()],
            },
        )
        await repo.record_event(document_id, "upload", "ok", detail={"bytes": len(data)})
        # Outbox, not a direct enqueue: the job is published only if this
        # transaction commits, so we can never queue a document that doesn't exist.
        await repo.enqueue_outbox(
            "document.uploaded", document_id, {"tenant_id": principal.tenant_id}
        )

    log.info(
        "ingestion.queued",
        document_id=str(document_id),
        filename=file.filename,
        bytes=len(data),
    )
    return UploadResponse(
        document_id=document_id,
        status=DocumentStatus.QUEUED,
        filename=file.filename or "upload",
        size_bytes=len(data),
        content_sha256=digest,
        s3_uri=f"s3://{stored['bucket']}/{stored['key']}",
        message="Queued for processing. Poll the status endpoint for progress.",
    )


@router.post(
    "/documents/presign",
    response_model=PresignResponse,
    summary="Get a presigned upload URL",
    dependencies=[Depends(require_scope("documents:write"))],
)
async def presign_upload(payload: PresignRequest, principal: PrincipalDep) -> PresignResponse:
    """For large files. The client PUTs straight to S3, so multi-GB bodies
    never touch an API pod, then calls the complete endpoint."""
    document_id = uuid.uuid4()
    store = get_object_store()
    key = store.build_key(principal.tenant_id, str(document_id), payload.filename)
    url = await store.presigned_put(key, payload.content_type)

    async with session_scope() as session:
        await DocumentRepository(session).create(
            id=document_id,
            tenant_id=principal.tenant_id,
            uploaded_by=principal.subject,
            filename=payload.filename,
            content_type=payload.content_type,
            size_bytes=payload.size_bytes,
            content_sha256=f"pending-{document_id}",
            s3_bucket=settings.s3_raw_bucket,
            s3_key=key,
            status=DocumentStatus.RECEIVED,
            classification=payload.metadata.classification,
            doc_metadata=payload.metadata.model_dump(mode="json"),
        )

    return PresignResponse(
        document_id=document_id,
        upload_url=url,
        s3_key=key,
        expires_in_seconds=settings.s3_presign_ttl_seconds,
        complete_url=f"{settings.api_prefix}/ingestion/documents/{document_id}/complete",
    )


@router.post(
    "/documents/{document_id}/complete",
    response_model=AcceptedResponse,
    summary="Confirm a presigned upload",
    dependencies=[Depends(require_scope("documents:write"))],
)
async def complete_upload(document_id: uuid.UUID, principal: PrincipalDep) -> AcceptedResponse:
    """Verify the object actually landed in S3, then queue processing."""
    async with session_scope() as session:
        repo = DocumentRepository(session)
        doc = await repo.get(document_id, tenant_id=principal.tenant_id)
        if doc is None:
            raise NotFoundError("Document not found")

        head = await get_object_store().head(doc.s3_key, bucket=doc.s3_bucket)
        if head is None:
            raise ValidationError("No object found at the presigned key; upload it first")

        await repo.set_status(
            document_id,
            DocumentStatus.QUEUED,
            size_bytes=head.get("ContentLength", doc.size_bytes),
            s3_version_id=head.get("VersionId"),
        )
        await repo.record_event(document_id, "upload", "ok", detail={"via": "presigned"})
        await repo.enqueue_outbox(
            "document.uploaded", document_id, {"tenant_id": principal.tenant_id}
        )

    return AcceptedResponse(
        message="Upload confirmed and queued for processing.", reference=str(document_id)
    )


@router.get(
    "/documents",
    response_model=DocumentListResponse,
    summary="List documents",
    dependencies=[Depends(require_scope("documents:read"))],
)
async def list_documents(
    principal: PrincipalDep,
    doc_status: Annotated[str | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> DocumentListResponse:
    async with session_scope() as session:
        rows, total = await DocumentRepository(session).list(
            principal.tenant_id, status=doc_status, limit=limit, offset=offset
        )
    return DocumentListResponse(
        total=total, limit=limit, offset=offset, documents=[_to_out(d) for d in rows]
    )


@router.get(
    "/documents/{document_id}",
    response_model=DocumentOut,
    summary="Get a document",
    dependencies=[Depends(require_scope("documents:read"))],
)
async def get_document(document_id: uuid.UUID, principal: PrincipalDep) -> DocumentOut:
    async with session_scope() as session:
        doc = await DocumentRepository(session).get(document_id, tenant_id=principal.tenant_id)
    if doc is None:
        raise NotFoundError("Document not found")
    return _to_out(doc)


@router.get(
    "/documents/{document_id}/status",
    response_model=DocumentStatusResponse,
    summary="Pipeline progress",
    dependencies=[Depends(require_scope("documents:read"))],
)
async def document_status(
    document_id: uuid.UUID, principal: PrincipalDep
) -> DocumentStatusResponse:
    """Per-stage history: fetch, parse, chunk, embed, persist, index — with
    timings. This is the endpoint to look at when an ingest is slow."""
    from sqlalchemy import select

    from app.db.models import IngestionEvent

    async with session_scope() as session:
        doc = await DocumentRepository(session).get(document_id, tenant_id=principal.tenant_id)
        if doc is None:
            raise NotFoundError("Document not found")
        events = (
            await session.execute(
                select(IngestionEvent)
                .where(IngestionEvent.document_id == document_id)
                .order_by(IngestionEvent.created_at)
            )
        ).scalars().all()

    return DocumentStatusResponse(
        document_id=document_id,
        status=doc.status,
        chunk_count=doc.chunk_count,
        indexed_count=doc.indexed_count,
        failure_reason=doc.failure_reason,
        events=[
            IngestionEventOut(
                stage=e.stage, status=e.status, duration_ms=e.duration_ms,
                detail=e.detail, created_at=e.created_at,
            )
            for e in events
        ],
    )


@router.post(
    "/documents/{document_id}/reindex",
    response_model=AcceptedResponse,
    summary="Re-index from stored embeddings",
    dependencies=[Depends(require_scope("documents:write"))],
)
async def reindex_document(
    document_id: uuid.UUID,
    principal: PrincipalDep,
    target_index: Annotated[str | None, Query()] = None,
) -> AcceptedResponse:
    """Rebuild OpenSearch documents from the pgvector copies. No re-embedding,
    so a mapping change costs storage time only, not model spend."""
    from arq import create_pool

    from app.workers.queue import JOB_REINDEX_DOCUMENT, redis_settings

    async with session_scope() as session:
        doc = await DocumentRepository(session).get(document_id, tenant_id=principal.tenant_id)
    if doc is None:
        raise NotFoundError("Document not found")

    pool = await create_pool(redis_settings())
    await pool.enqueue_job(
        JOB_REINDEX_DOCUMENT,
        str(document_id),
        target_index or settings.opensearch_index_alias,
        _queue_name=settings.ingestion_queue,
    )
    return AcceptedResponse(message="Re-index queued.", reference=str(document_id))


@router.delete(
    "/documents/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a document everywhere",
    dependencies=[Depends(require_scope("documents:write"))],
)
async def delete_document(document_id: uuid.UUID, principal: PrincipalDep) -> None:
    """Purge order matters: index first, then object, then row. If a step fails
    the row survives and the delete can be retried; the reverse would orphan
    vectors with no record of what they were."""
    from app.vector.hybrid_search import HybridSearcher

    async with session_scope() as session:
        repo = DocumentRepository(session)
        doc = await repo.get(document_id, tenant_id=principal.tenant_id)
        if doc is None:
            raise NotFoundError("Document not found")
        bucket, key = doc.s3_bucket, doc.s3_key

    deleted = await HybridSearcher().delete_document(str(document_id), principal.tenant_id)
    await get_object_store().delete(key, bucket=bucket)

    async with session_scope() as session:
        doc = await DocumentRepository(session).get(document_id, tenant_id=principal.tenant_id)
        if doc is not None:
            await session.delete(doc)

    log.info("ingestion.document_deleted", document_id=str(document_id), vectors=deleted)


@router.post(
    "/search",
    response_model=SearchResponse,
    summary="Hybrid search the knowledge base",
    dependencies=[Depends(require_scope("documents:read"))],
)
async def search(payload: SearchRequestIn, principal: PrincipalDep) -> SearchResponse:
    """Direct access to retrieval, bypassing the agents.

    Useful for three things: debugging why an agent gave a bad answer,
    building eval sets, and letting a UI offer document search alongside chat.
    """
    from app.llm.gateway import get_gateway
    from app.vector.hybrid_search import HybridSearcher, SearchRequest

    started = time.perf_counter()
    embedding = None
    if payload.strategy in {"hybrid", "knn"}:
        vectors, _ = await get_gateway().embed([payload.query])
        embedding = vectors[0]

    filters: dict = {}
    if payload.doc_type:
        filters["doc_type"] = payload.doc_type
    if payload.tags:
        filters["tags"] = payload.tags

    hits = await HybridSearcher().search(
        SearchRequest(
            query=payload.query,
            embedding=embedding,
            tenant_id=principal.tenant_id,
            top_k=payload.top_k,
            filters=filters,
            strategy=payload.strategy,
            rerank=payload.rerank,
            compress=payload.compress,
        )
    )

    return SearchResponse(
        query=payload.query,
        strategy=payload.strategy,
        returned=len(hits),
        took_ms=int((time.perf_counter() - started) * 1000),
        hits=[
            SearchHitOut(
                chunk_id=h.chunk_id,
                document_id=h.document_id,
                content=h.content,
                score=h.score,
                rerank_score=h.rerank_score,
                filename=h.filename,
                page=h.page_number,
                section=h.section_path,
                bm25_rank=h.bm25_rank,
                knn_rank=h.knn_rank,
            )
            for h in hits
        ],
    )
