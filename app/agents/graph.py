"""The agent graph.

    guardrail
        |
    coordinator ──────────────► synthesis      (small talk / out of scope / blocked)
        ├── accounts ─────────► synthesis
        ├── transactions ─────► synthesis
        └── service ──┬───────► synthesis
                      └── approval ─► synthesis   (only when an action is staged)

Compiled once at startup and reused. The checkpointer is bound at compile time,
which is what gives every thread durable state and makes the approval interrupt
resumable across process restarts.
"""
from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from app.agents.nodes import (
    accounts_node,
    approval_node,
    coordinator_node,
    guardrail_node,
    service_node,
    synthesis_node,
    transactions_node,
)
from app.agents.state import AgentState
from app.core.config import settings
from app.core.constants import AgentName
from app.core.logging import get_logger

log = get_logger(__name__)

_compiled: Any = None


def route_after_guardrail(state: AgentState) -> str:
    return "synthesis" if state.get("error") == "guardrail_blocked" else "coordinator"


def route_after_coordinator(state: AgentState) -> str:
    if state.get("error") or state.get("final_answer"):
        return "synthesis"
    target = state.get("target_agent", AgentName.COORDINATOR)
    return {
        AgentName.ACCOUNTS: "accounts",
        AgentName.TRANSACTIONS: "transactions",
        AgentName.SERVICE: "service",
    }.get(target, "synthesis")


def route_after_service(state: AgentState) -> str:
    """Only detour through the approval gate when an action was actually staged."""
    if (
        settings.enable_human_in_the_loop
        and state.get("requires_approval")
        and state.get("staged_action")
    ):
        return "approval"
    return "synthesis"


def build_graph() -> StateGraph:
    graph = StateGraph(AgentState)

    graph.add_node("guardrail", guardrail_node)
    graph.add_node("coordinator", coordinator_node)
    graph.add_node("accounts", accounts_node)
    graph.add_node("transactions", transactions_node)
    graph.add_node("service", service_node)
    graph.add_node("approval", approval_node)
    graph.add_node("synthesis", synthesis_node)

    graph.add_edge(START, "guardrail")
    graph.add_conditional_edges(
        "guardrail", route_after_guardrail, {"coordinator": "coordinator", "synthesis": "synthesis"}
    )
    graph.add_conditional_edges(
        "coordinator",
        route_after_coordinator,
        {
            "accounts": "accounts",
            "transactions": "transactions",
            "service": "service",
            "synthesis": "synthesis",
        },
    )
    graph.add_edge("accounts", "synthesis")
    graph.add_edge("transactions", "synthesis")
    graph.add_conditional_edges(
        "service", route_after_service, {"approval": "approval", "synthesis": "synthesis"}
    )
    graph.add_edge("approval", "synthesis")
    graph.add_edge("synthesis", END)

    return graph


def compile_graph(checkpointer: Any = None) -> Any:
    global _compiled
    from app.memory.checkpointer import get_checkpointer

    _compiled = build_graph().compile(checkpointer=checkpointer or get_checkpointer())
    log.info(
        "graph.compiled",
        nodes=["guardrail", "coordinator", "accounts", "transactions", "service", "approval", "synthesis"],
        checkpointer=settings.checkpointer,
    )
    return _compiled


def get_graph() -> Any:
    if _compiled is None:
        return compile_graph()
    return _compiled


def render_mermaid() -> str:
    """Emit the graph as Mermaid -- useful in docs and PR descriptions."""
    try:
        return get_graph().get_graph().draw_mermaid()
    except Exception:  # noqa: BLE001
        return "graph TD; guardrail-->coordinator-->accounts & transactions & service-->synthesis"
