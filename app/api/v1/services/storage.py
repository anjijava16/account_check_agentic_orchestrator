"""Object storage (MinIO / S3) CRUD.

Operations are restricted to the two configured buckets so this operator surface
can't be pointed at arbitrary buckets. Object keys are treated as opaque paths.
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, UploadFile, status

from app.api.deps import require_role
from app.core.config import settings
from app.core.exceptions import StorageError
from app.core.logging import get_logger
from app.storage.s3 import get_object_store, sha256_of

log = get_logger(__name__)
router = APIRouter(
    prefix="/services/storage",
    tags=["services: object-store"],
    dependencies=[Depends(require_role("agent_operator"))],
)


def _resolve_bucket(bucket: str | None) -> str:
    allowed = {settings.s3_raw_bucket, settings.s3_processed_bucket}
    chosen = bucket or settings.s3_raw_bucket
    if chosen not in allowed:
        raise HTTPException(
            status_code=400, detail=f"Bucket must be one of: {', '.join(sorted(allowed))}"
        )
    return chosen


@router.get("/objects", summary="List objects by prefix")
async def list_objects(
    prefix: Annotated[str, Query(max_length=1024)] = "",
    bucket: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> dict[str, Any]:
    b = _resolve_bucket(bucket)
    items = await get_object_store().list_objects(prefix=prefix, bucket=b, max_keys=limit)
    return {"bucket": b, "prefix": prefix, "count": len(items), "objects": items}


@router.post("/objects", status_code=status.HTTP_201_CREATED, summary="Create (upload) an object")
async def create_object(
    file: UploadFile,
    key: Annotated[str | None, Query(max_length=1024)] = None,
    bucket: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    b = _resolve_bucket(bucket)
    data = await file.read()
    object_key = key or file.filename
    if not object_key:
        raise HTTPException(status_code=400, detail="A key or filename is required")
    try:
        result = await get_object_store().put(
            key=object_key,
            data=data,
            content_type=file.content_type or "application/octet-stream",
            bucket=b,
            metadata={"sha256": sha256_of(data)},
        )
    except StorageError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"bucket": b, "key": object_key, "size_bytes": len(data), **result}


@router.get("/objects/{key:path}", summary="Read (download) an object")
async def read_object(
    key: str,
    bucket: Annotated[str | None, Query()] = None,
    presign: Annotated[bool, Query()] = False,
) -> Any:
    b = _resolve_bucket(bucket)
    store = get_object_store()
    if await store.head(key, bucket=b) is None:
        raise HTTPException(status_code=404, detail=f"Object not found: {key}")
    if presign:
        url = await store.presigned_get(key, bucket=b)
        return {"bucket": b, "key": key, "url": url}
    try:
        data = await store.get(key, bucket=b)
    except StorageError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return Response(content=data, media_type="application/octet-stream")


@router.put("/objects/{key:path}", summary="Update (overwrite) an object")
async def update_object(
    key: str,
    file: UploadFile,
    bucket: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    b = _resolve_bucket(bucket)
    data = await file.read()
    try:
        result = await get_object_store().put(
            key=key,
            data=data,
            content_type=file.content_type or "application/octet-stream",
            bucket=b,
            metadata={"sha256": sha256_of(data)},
        )
    except StorageError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"bucket": b, "key": key, "size_bytes": len(data), **result}


@router.delete("/objects/{key:path}", summary="Delete an object")
async def delete_object(
    key: str, bucket: Annotated[str | None, Query()] = None
) -> dict[str, Any]:
    b = _resolve_bucket(bucket)
    store = get_object_store()
    if await store.head(key, bucket=b) is None:
        raise HTTPException(status_code=404, detail=f"Object not found: {key}")
    await store.delete(key, bucket=b)
    return {"bucket": b, "key": key, "deleted": True}


@router.get("/objects/{key:path}/_meta", summary="Object metadata (HEAD)")
async def object_meta(
    key: str, bucket: Annotated[str | None, Query()] = None
) -> dict[str, Any]:
    b = _resolve_bucket(bucket)
    head = await get_object_store().head(key, bucket=b)
    if head is None:
        raise HTTPException(status_code=404, detail=f"Object not found: {key}")
    return {
        "bucket": b,
        "key": key,
        "size_bytes": head.get("ContentLength"),
        "content_type": head.get("ContentType"),
        "etag": head.get("ETag", "").strip('"'),
        "last_modified": head.get("LastModified").isoformat()
        if head.get("LastModified")
        else None,
        "metadata": head.get("Metadata", {}),
    }
