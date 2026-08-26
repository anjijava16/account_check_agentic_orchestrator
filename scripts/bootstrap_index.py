#!/usr/bin/env python3
"""Create the OpenSearch index and alias if they don't exist."""
from __future__ import annotations

import asyncio

from app.core.config import settings
from app.core.logging import configure_logging, get_logger
from app.vector.opensearch_client import close_client, ensure_index, index_stats, ping

log = get_logger(__name__)


async def main() -> None:
    configure_logging(json_output=False)
    if not await ping():
        log.error("opensearch.unreachable", hosts=settings.opensearch_hosts)
        return
    index = await ensure_index()
    stats = await index_stats(settings.opensearch_index_alias)
    log.info("index.ready", index=index, alias=settings.opensearch_index_alias, stats=stats)
    await close_client()


if __name__ == "__main__":
    asyncio.run(main())
