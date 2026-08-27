"""Agent graph introspection.

Read-only view of the agent topology: agents, the closed intent set, the
intent-to-agent routing table, high-risk (approval-gated) intents, and the
compiled graph's nodes.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import current_principal
from app.core.constants import HIGH_RISK_INTENTS, INTENT_TO_AGENT, AgentName, Intent
from app.core.logging import get_logger

log = get_logger(__name__)
router = APIRouter(
    prefix="/services/agents",
    tags=["services: agents"],
    dependencies=[Depends(current_principal)],
)

# Which MCP server each specialist agent is bound to.
_AGENT_SERVERS: dict[str, list[str]] = {
    AgentName.ACCOUNTS: ["accounts"],
    AgentName.TRANSACTIONS: ["transactions"],
    AgentName.SERVICE: ["service"],
    AgentName.COORDINATOR: [],
}


@router.get("", summary="List agents, intents and routing")
async def list_agents() -> dict[str, Any]:
    return {
        "agents": [a.value for a in AgentName],
        "intents": [i.value for i in Intent],
        "intent_to_agent": {i.value: a.value for i, a in INTENT_TO_AGENT.items()},
        "high_risk_intents": sorted(i.value for i in HIGH_RISK_INTENTS),
    }


@router.get("/graph", summary="Compiled graph nodes")
async def graph_nodes() -> dict[str, Any]:
    from app.agents.graph import get_graph

    try:
        compiled = get_graph()
        nodes = sorted(compiled.get_graph().nodes.keys())
    except Exception:  # noqa: BLE001 -- graph not compiled outside app lifespan
        nodes = [
            "guardrail", "coordinator", "accounts",
            "transactions", "service", "approval", "synthesis",
        ]
    return {"nodes": nodes, "entry": "guardrail", "terminal": "synthesis"}


@router.get("/{agent}", summary="Details for one agent")
async def agent_detail(agent: str) -> dict[str, Any]:
    if agent not in {a.value for a in AgentName}:
        raise HTTPException(status_code=404, detail=f"Unknown agent: {agent}")
    intents = [i.value for i, a in INTENT_TO_AGENT.items() if a == agent]
    return {
        "agent": agent,
        "mcp_servers": _AGENT_SERVERS.get(agent, []),
        "handles_intents": intents,
    }
