"""Repository for the cost ledger + daily budget enforcement."""
from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CostRecord, DailyBudget


class CostRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def record(self, **fields: Any) -> CostRecord:
        fields.setdefault("usage_date", datetime.now(UTC).date())
        record = CostRecord(**fields)
        self.session.add(record)
        await self.session.flush()
        await self._accrue_budget(
            fields["tenant_id"], fields["usage_date"], Decimal(str(fields.get("cost_usd", 0)))
        )
        return record

    async def _accrue_budget(self, tenant_id: str, usage_date: date, amount: Decimal) -> None:
        stmt = pg_insert(DailyBudget).values(
            tenant_id=tenant_id, usage_date=usage_date, limit_usd=0, spent_usd=amount
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_budget_tenant_day",
            set_={"spent_usd": DailyBudget.spent_usd + amount},
        )
        await self.session.execute(stmt)

    async def spend_today(self, tenant_id: str) -> Decimal:
        stmt = select(func.coalesce(func.sum(CostRecord.cost_usd), 0)).where(
            CostRecord.tenant_id == tenant_id,
            CostRecord.usage_date == datetime.now(UTC).date(),
        )
        return Decimal(str((await self.session.execute(stmt)).scalar_one()))

    async def session_cost(self, session_id: uuid.UUID) -> Decimal:
        stmt = select(func.coalesce(func.sum(CostRecord.cost_usd), 0)).where(
            CostRecord.session_id == session_id
        )
        return Decimal(str((await self.session.execute(stmt)).scalar_one()))

    async def breakdown(self, tenant_id: str, days: int = 7) -> list[dict[str, Any]]:
        stmt = (
            select(
                CostRecord.usage_date,
                CostRecord.model,
                CostRecord.deployment,
                CostRecord.call_type,
                func.sum(CostRecord.total_tokens).label("tokens"),
                func.sum(CostRecord.cost_usd).label("cost_usd"),
                func.count().label("calls"),
                func.avg(CostRecord.latency_ms).label("avg_latency_ms"),
            )
            .where(
                CostRecord.tenant_id == tenant_id,
                CostRecord.usage_date >= func.current_date() - days,
            )
            .group_by(
                CostRecord.usage_date,
                CostRecord.model,
                CostRecord.deployment,
                CostRecord.call_type,
            )
            .order_by(CostRecord.usage_date.desc())
        )
        return [dict(r._mapping) for r in (await self.session.execute(stmt)).all()]

    async def set_limit(self, tenant_id: str, usage_date: date, limit_usd: float) -> None:
        stmt = pg_insert(DailyBudget).values(
            tenant_id=tenant_id, usage_date=usage_date, limit_usd=limit_usd
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_budget_tenant_day", set_={"limit_usd": limit_usd}
        )
        await self.session.execute(stmt)

    async def mark_alerted(self, tenant_id: str, usage_date: date) -> None:
        await self.session.execute(
            update(DailyBudget)
            .where(DailyBudget.tenant_id == tenant_id, DailyBudget.usage_date == usage_date)
            .values(alert_sent=True)
        )
