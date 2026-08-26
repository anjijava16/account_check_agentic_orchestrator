"""Repository for agent steps + tool invocations (Observability panel)."""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AgentStep, ToolInvocation


def _preview(text: str | None, limit: int = 2000) -> str | None:
    if text is None:
        return None
    return text if len(text) <= limit else text[:limit] + "…"


class TraceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add_step(self, **fields: Any) -> AgentStep:
        fields["prompt_preview"] = _preview(fields.get("prompt_preview"))
        fields["output_preview"] = _preview(fields.get("output_preview"))
        step = AgentStep(**fields)
        self.session.add(step)
        await self.session.flush()
        return step

    async def add_tool_invocation(self, **fields: Any) -> ToolInvocation:
        fields["result_preview"] = _preview(fields.get("result_preview"))
        inv = ToolInvocation(**fields)
        self.session.add(inv)
        await self.session.flush()
        return inv

    async def steps_for_session(self, session_id: uuid.UUID) -> list[AgentStep]:
        stmt = (
            select(AgentStep)
            .where(AgentStep.session_id == session_id)
            .order_by(AgentStep.created_at, AgentStep.step_index)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def tool_stats(self, days: int = 1) -> list[dict[str, Any]]:
        stmt = (
            select(
                ToolInvocation.mcp_server,
                ToolInvocation.tool_name,
                func.count().label("calls"),
                func.count().filter(ToolInvocation.status != "ok").label("errors"),
                func.percentile_cont(0.95)
                .within_group(ToolInvocation.duration_ms)
                .label("p95_ms"),
            )
            .where(ToolInvocation.created_at >= func.now() - text(f"interval '{int(days)} days'"))
            .group_by(ToolInvocation.mcp_server, ToolInvocation.tool_name)
        )
        return [dict(r._mapping) for r in (await self.session.execute(stmt)).all()]
