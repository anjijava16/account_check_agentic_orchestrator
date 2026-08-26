"""LangGraph checkpointer factory.

Postgres checkpointing is what makes human-in-the-loop possible: the graph is
interrupted before a high-risk tool call, the state is durably persisted, and
the run resumes hours later from the exact same node after an approval.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

_checkpointer: Any = None


@asynccontextmanager
async def checkpointer_lifespan():
    """Bind the checkpointer for the lifetime of the process."""
    global _checkpointer
    if settings.checkpointer == "postgres":
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        conn_str = settings.postgres_sync_dsn.replace("postgresql+psycopg", "postgresql")
        async with AsyncPostgresSaver.from_conn_string(conn_str) as saver:
            await saver.setup()
            _checkpointer = saver
            log.info("checkpointer.ready", backend="postgres")
            yield saver
    else:
        from langgraph.checkpoint.memory import MemorySaver

        _checkpointer = MemorySaver()
        log.warning("checkpointer.ready", backend="memory", note="not durable")
        yield _checkpointer
    _checkpointer = None


def get_checkpointer() -> Any:
    if _checkpointer is None:
        from langgraph.checkpoint.memory import MemorySaver

        return MemorySaver()
    return _checkpointer
