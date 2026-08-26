"""Cost Tracker.

Every LLMUsage produced by the gateway lands here. Writes are buffered in
memory and flushed in batches so a chat turn never blocks on a ledger insert,
and daily spend is checked against the tenant budget on each flush.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.core.config import settings
from app.core.logging import get_logger
from app.db.repositories.cost import CostRepository
from app.db.session import session_scope
from app.llm.gateway import LLMUsage
from app.observability.metrics import LLM_COST

log = get_logger(__name__)


class CostTracker:
    def __init__(self, flush_interval: float = 2.0, max_buffer: int = 100) -> None:
        self._buffer: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()
        self._flush_interval = flush_interval
        self._max_buffer = max_buffer
        self._task: asyncio.Task | None = None
        self._stopping = False

    async def start(self) -> None:
        if settings.cost_tracking_enabled and self._task is None:
            self._task = asyncio.create_task(self._flush_loop(), name="cost-tracker-flush")
            log.info("cost_tracker.started")

    async def stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self.flush()
        log.info("cost_tracker.stopped")

    async def track(
        self,
        usage: LLMUsage,
        *,
        tenant_id: str,
        user_id: str | None = None,
        session_id: uuid.UUID | None = None,
        request_id: str | None = None,
        agent: str | None = None,
        call_type: str = "chat",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if not settings.cost_tracking_enabled:
            return
        LLM_COST.labels(model=usage.model, deployment=usage.deployment).inc(usage.cost_usd)
        row = {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "session_id": session_id,
            "request_id": request_id,
            "usage_date": datetime.now(UTC).date(),
            "call_type": call_type,
            "agent": agent,
            "model": usage.model,
            "provider": usage.provider,
            "deployment": usage.deployment,
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
            "total_tokens": usage.total_tokens,
            "cached_tokens": usage.cached_tokens,
            "cost_usd": usage.cost_usd,
            "latency_ms": usage.latency_ms,
            "success": usage.success,
            "cost_metadata": {"fallback_used": usage.fallback_used, **(metadata or {})},
        }
        async with self._lock:
            self._buffer.append(row)
            should_flush = len(self._buffer) >= self._max_buffer
        if should_flush:
            await self.flush()

    async def flush(self) -> None:
        async with self._lock:
            batch, self._buffer = self._buffer, []
        if not batch:
            return
        try:
            async with session_scope() as session:
                repo = CostRepository(session)
                for row in batch:
                    await repo.record(**row)
            log.debug("cost_tracker.flushed", rows=len(batch))
        except Exception as exc:  # noqa: BLE001
            log.error("cost_tracker.flush_failed", error=str(exc), rows=len(batch))

    async def _flush_loop(self) -> None:
        while not self._stopping:
            await asyncio.sleep(self._flush_interval)
            await self.flush()
            await self._check_budget()

    async def _check_budget(self) -> None:
        try:
            async with session_scope() as session:
                repo = CostRepository(session)
                spend = await repo.spend_today("default")
                if spend > Decimal(str(settings.cost_alert_daily_usd)):
                    log.warning(
                        "cost_tracker.budget_exceeded",
                        spend_usd=float(spend),
                        limit_usd=settings.cost_alert_daily_usd,
                    )
                    await repo.mark_alerted("default", datetime.now(UTC).date())
        except Exception:  # noqa: BLE001, S110
            pass


_tracker: CostTracker | None = None


def get_cost_tracker() -> CostTracker:
    global _tracker
    if _tracker is None:
        _tracker = CostTracker()
    return _tracker
