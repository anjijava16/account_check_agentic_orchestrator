"""arq queue definition -- Redis-backed job queue for background ingestion."""
from __future__ import annotations

from arq.connections import RedisSettings

from app.core.config import settings


def redis_settings() -> RedisSettings:
    return RedisSettings.from_dsn(str(settings.redis_dsn))


JOB_INGEST_DOCUMENT = "ingest_document"
JOB_REINDEX_DOCUMENT = "reindex_document"
JOB_PUBLISH_OUTBOX = "publish_outbox"
JOB_RUN_ONLINE_EVAL = "run_online_eval"
JOB_REFRESH_STATS = "refresh_stats"
