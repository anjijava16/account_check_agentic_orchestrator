"""arq worker entrypoint.

Run with:  arq app.workers.worker.WorkerSettings
"""
from __future__ import annotations

from typing import Any

from app.core.config import settings
from app.core.logging import configure_logging, get_logger
from app.db.session import dispose_engine
from app.ingestion.pipeline import IngestionPipeline
from app.llm.cost_tracker import get_cost_tracker
from app.memory.redis_client import close_redis
from app.observability.tracing import setup_tracing
from app.storage.s3 import get_object_store
from app.vector.opensearch_client import close_client, ensure_index
from app.workers.queue import redis_settings
from app.workers.tasks import (
    ingest_document,
    publish_outbox,
    refresh_stats,
    reindex_document,
    run_online_eval,
)

log = get_logger(__name__)


async def startup(ctx: dict[str, Any]) -> None:
    configure_logging(settings.log_level, settings.log_json)
    setup_tracing()
    await get_object_store().ensure_buckets()
    await ensure_index()
    await get_cost_tracker().start()
    ctx["pipeline"] = IngestionPipeline()
    log.info("worker.started", queue=settings.ingestion_queue)


async def shutdown(ctx: dict[str, Any]) -> None:
    await get_cost_tracker().stop()
    await close_client()
    await close_redis()
    await dispose_engine()
    log.info("worker.stopped")


class WorkerSettings:
    functions = [ingest_document, reindex_document, run_online_eval]
    cron_jobs: list = []
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = redis_settings()
    queue_name = settings.ingestion_queue
    max_jobs = 8
    job_timeout = 900
    keep_result = 3600
    max_tries = settings.ingestion_max_retries
    retry_jobs = True
    health_check_interval = 30


def _register_cron() -> None:
    from arq import cron

    WorkerSettings.cron_jobs = [
        cron(publish_outbox, second={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}, run_at_startup=True),
        cron(refresh_stats, second={0, 30}),
    ]


_register_cron()
