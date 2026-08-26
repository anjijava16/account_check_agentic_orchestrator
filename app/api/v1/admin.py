"""AdminRouter.

Operational surface for the platform team: cost reporting, index management,
graph introspection, and MCP tool inventory. Restricted to operators.
"""
from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app.api.deps import PrincipalDep, require_role
from app.core.config import settings
from app.core.logging import get_logger
from app.db.repositories.cost import CostRepository
from app.db.repositories.traces import TraceRepository
from app.db.session import session_scope

log = get_logger(__name__)
router = APIRouter(
    prefix="/admin", tags=["admin"], dependencies=[Depends(require_role("agent_operator"))]
)


class BudgetRequest(BaseModel):
    limit_usd: float = Field(gt=0, le=1_000_000)
    usage_date: date | None = None


@router.get("/cost/summary", summary="Spend summary")
async def cost_summary(
    principal: PrincipalDep, days: Annotated[int, Query(ge=1, le=90)] = 7
) -> dict[str, Any]:
    """Spend broken down by day, model, deployment and call type.

    Deployment split is the number that matters: it tells you how much traffic
    is going to a third-party model that could have been served self-hosted.
    """
    async with session_scope() as session:
        repo = CostRepository(session)
        rows = await repo.breakdown(principal.tenant_id, days=days)
        today = await repo.spend_today(principal.tenant_id)

    by_deployment: dict[str, float] = {}
    for row in rows:
        key = row.get("deployment") or "unknown"
        by_deployment[key] = round(by_deployment.get(key, 0.0) + float(row["cost_usd"]), 4)

    return {
        "tenant_id": principal.tenant_id,
        "window_days": days,
        "spend_today_usd": float(today),
        "daily_limit_usd": settings.cost_alert_daily_usd,
        "by_deployment_usd": by_deployment,
        "breakdown": [
            {
                "date": str(r["usage_date"]),
                "model": r["model"],
                "deployment": r["deployment"],
                "call_type": r["call_type"],
                "calls": int(r["calls"]),
                "tokens": int(r["tokens"] or 0),
                "cost_usd": round(float(r["cost_usd"]), 6),
                "avg_latency_ms": round(float(r["avg_latency_ms"] or 0), 1),
            }
            for r in rows
        ],
    }


@router.get("/cost/session/{session_id}", summary="Cost for one conversation")
async def session_cost(session_id: uuid.UUID) -> dict[str, Any]:
    async with session_scope() as session:
        total = await CostRepository(session).session_cost(session_id)
    return {"session_id": str(session_id), "cost_usd": float(total)}


@router.put("/cost/budget", summary="Set the daily budget")
async def set_budget(payload: BudgetRequest, principal: PrincipalDep) -> dict[str, Any]:
    target = payload.usage_date or datetime.now(UTC).date()
    async with session_scope() as session:
        await CostRepository(session).set_limit(principal.tenant_id, target, payload.limit_usd)
    return {"tenant_id": principal.tenant_id, "date": str(target), "limit_usd": payload.limit_usd}


@router.get("/traces/session/{session_id}", summary="Agent steps for a conversation")
async def session_trace(session_id: uuid.UUID) -> dict[str, Any]:
    """Every node execution for a conversation, in order, with timings. This is
    the first thing to open when a customer says the assistant got it wrong."""
    async with session_scope() as session:
        steps = await TraceRepository(session).steps_for_session(session_id)
    return {
        "session_id": str(session_id),
        "steps": [
            {
                "index": s.step_index,
                "node": s.node,
                "agent": s.agent,
                "status": s.status,
                "duration_ms": s.duration_ms,
                "trace_id": s.trace_id,
                "output_preview": s.output_preview,
                "at": s.created_at.isoformat(),
            }
            for s in steps
        ],
    }


@router.get("/tools", summary="MCP tool inventory")
async def tool_inventory() -> dict[str, Any]:
    """What tools each MCP server is currently advertising, and which servers
    are reachable. If an agent 'forgot' a capability, check here first."""
    from app.mcp.client.manager import SERVER_CONFIG, get_mcp_manager

    manager = get_mcp_manager()
    inventory: dict[str, Any] = {}
    for server in SERVER_CONFIG:
        tools = manager._tools_by_server.get(server, [])  # noqa: SLF001
        inventory[server] = {
            "url": SERVER_CONFIG[server]["url"],
            "healthy": manager.health.get(server, False),
            "tools": [{"name": t.name, "description": t.description} for t in tools],
        }
    return {"servers": inventory}


@router.post("/tools/reconnect", summary="Reconnect to MCP servers")
async def reconnect_tools() -> dict[str, Any]:
    from app.mcp.client.manager import get_mcp_manager

    manager = get_mcp_manager()
    await manager.connect()
    return {"health": manager.health}


@router.get("/index/stats", summary="OpenSearch index stats")
async def index_statistics() -> dict[str, Any]:
    from app.vector.opensearch_client import index_stats, resolve_alias

    alias = settings.opensearch_index_alias
    return {
        "alias": alias,
        "physical_index": await resolve_alias(alias),
        "stats": await index_stats(alias),
    }


class AliasSwapRequest(BaseModel):
    new_index: str = Field(min_length=1)


@router.post("/index/swap-alias", summary="Repoint the search alias")
async def swap_index_alias(payload: AliasSwapRequest) -> dict[str, Any]:
    """The zero-downtime half of a re-embed: build the new index, backfill it,
    then flip the alias in one atomic call."""
    from app.vector.opensearch_client import swap_alias

    await swap_alias(payload.new_index)
    log.info("admin.alias_swapped", index=payload.new_index)
    return {"alias": settings.opensearch_index_alias, "now_points_to": payload.new_index}


@router.get("/graph", summary="Agent graph topology")
async def graph_topology() -> dict[str, Any]:
    from app.agents.graph import render_mermaid

    return {
        "mermaid": render_mermaid(),
        "checkpointer": settings.checkpointer,
        "human_in_the_loop": settings.enable_human_in_the_loop,
        "max_tool_calls": settings.agent_max_tool_calls,
    }


@router.get("/config", summary="Effective (redacted) configuration")
async def effective_config() -> dict[str, Any]:
    """Non-secret settings only. Useful for confirming what a pod actually
    booted with, which is rarely what the Helm values say."""
    dump = settings.model_dump()
    for key in list(dump):
        if any(s in key for s in ("password", "secret", "key", "dsn", "token")):
            dump[key] = "***"
    return dump
