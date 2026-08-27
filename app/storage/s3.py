"""S3 / MinIO object storage.

Raw uploads land here first and the request returns immediately -- parsing and
embedding happen out of band. The bucket is the system of record for bytes;
Postgres holds the pointer and the lifecycle state.
"""
from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import aioboto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app.core.config import settings
from app.core.exceptions import StorageError
from app.core.logging import get_logger

log = get_logger(__name__)

_BOTO_CONFIG = Config(
    retries={"max_attempts": 5, "mode": "adaptive"},
    max_pool_connections=50,
    signature_version="s3v4",
)


class ObjectStore:
    def __init__(self) -> None:
        self._session = aioboto3.Session()

    @asynccontextmanager
    async def _client(self) -> AsyncIterator[Any]:
        async with self._session.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            region_name=settings.s3_region,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
            config=_BOTO_CONFIG,
        ) as client:
            yield client

    # ------------------------------------------------------------ lifecycle
    async def ensure_buckets(self) -> None:
        async with self._client() as client:
            for bucket in (settings.s3_raw_bucket, settings.s3_processed_bucket):
                try:
                    await client.head_bucket(Bucket=bucket)
                except ClientError:
                    try:
                        await client.create_bucket(Bucket=bucket)
                        await client.put_bucket_versioning(
                            Bucket=bucket, VersioningConfiguration={"Status": "Enabled"}
                        )
                        log.info("s3.bucket_created", bucket=bucket)
                    except ClientError as exc:  # noqa: PERF203
                        log.warning("s3.bucket_create_failed", bucket=bucket, error=str(exc))

    # --------------------------------------------------------------- writes
    @staticmethod
    def build_key(tenant_id: str, document_id: str, filename: str) -> str:
        """Date-partitioned key: cheap lifecycle rules and fast prefix listing."""
        stamp = datetime.now(UTC)
        safe = filename.replace("/", "_").replace("\\", "_")[:200]
        return (
            f"tenant={tenant_id}/year={stamp:%Y}/month={stamp:%m}/day={stamp:%d}/"
            f"{document_id}/{safe}"
        )

    async def put(
        self,
        *,
        key: str,
        data: bytes,
        content_type: str,
        bucket: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        bucket = bucket or settings.s3_raw_bucket
        extra: dict[str, Any] = {
            "Bucket": bucket,
            "Key": key,
            "Body": data,
            "ContentType": content_type,
            "Metadata": {k: str(v)[:1024] for k, v in (metadata or {}).items()},
        }
        if settings.s3_sse_enabled:
            if settings.s3_sse_kms_key_id:
                extra["ServerSideEncryption"] = "aws:kms"
                extra["SSEKMSKeyId"] = settings.s3_sse_kms_key_id
            else:
                extra["ServerSideEncryption"] = "AES256"

        try:
            async with self._client() as client:
                response = await client.put_object(**extra)
        except ClientError as exc:
            raise StorageError(f"Upload failed for {key}: {exc}") from exc

        log.info("s3.object_written", bucket=bucket, key=key, bytes=len(data))
        return {
            "bucket": bucket,
            "key": key,
            "version_id": response.get("VersionId"),
            "etag": response.get("ETag", "").strip('"'),
        }

    async def get(self, key: str, *, bucket: str | None = None) -> bytes:
        bucket = bucket or settings.s3_raw_bucket
        try:
            async with self._client() as client:
                response = await client.get_object(Bucket=bucket, Key=key)
                return await response["Body"].read()
        except ClientError as exc:
            raise StorageError(f"Download failed for {key}: {exc}") from exc

    async def delete(self, key: str, *, bucket: str | None = None) -> None:
        bucket = bucket or settings.s3_raw_bucket
        async with self._client() as client:
            await client.delete_object(Bucket=bucket, Key=key)

    async def presigned_put(
        self, key: str, content_type: str, *, bucket: str | None = None
    ) -> str:
        """Direct-to-S3 upload URL for large files -- keeps multi-GB bodies out
        of the API pods entirely."""
        bucket = bucket or settings.s3_raw_bucket
        async with self._client() as client:
            return await client.generate_presigned_url(
                "put_object",
                Params={"Bucket": bucket, "Key": key, "ContentType": content_type},
                ExpiresIn=settings.s3_presign_ttl_seconds,
            )

    async def presigned_get(self, key: str, *, bucket: str | None = None) -> str:
        bucket = bucket or settings.s3_raw_bucket
        async with self._client() as client:
            return await client.generate_presigned_url(
                "get_object",
                Params={"Bucket": bucket, "Key": key},
                ExpiresIn=settings.s3_presign_ttl_seconds,
            )

    async def head(self, key: str, *, bucket: str | None = None) -> dict[str, Any] | None:
        bucket = bucket or settings.s3_raw_bucket
        try:
            async with self._client() as client:
                return await client.head_object(Bucket=bucket, Key=key)
        except ClientError:
            return None


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


_store: ObjectStore | None = None


def get_object_store() -> ObjectStore:
    global _store
    if _store is None:
        _store = ObjectStore()
    return _store
