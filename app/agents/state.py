"""Graph state.

One typed dict flows through every node. Rules that keep it manageable:
  * `messages` is append-only (LangGraph's add_messages reducer)
  * anything a downstream node needs is written here, never smuggled through
    closures -- that's what makes checkpoint/resume work
  * the state is serialised into the Postgres checkpointer on every super-step,
    so keep values JSON-friendly
"""
from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


class Citation(TypedDict, total=False):
    chunk_id: str
    document_id: str
    filename: str | None
    page: int | None
    section: str | None
    score: float


class ToolTrace(TypedDict, total=False):
    server: str
    tool: str
    arguments: dict[str, Any]
    status: str
    duration_ms: int


class AgentState(TypedDict, total=False):
    # ---- identity & routing -------------------------------------------
    session_id: str
    turn_id: str
    request_id: str
    tenant_id: str
    user_id: str
    customer_id: str | None
    roles: list[str]
    channel: str

    # ---- conversation --------------------------------------------------
    messages: Annotated[list[BaseMessage], add_messages]
    user_input: str
    sanitised_input: str

    # ---- coordinator decisions ----------------------------------------
    intent: str
    intent_confidence: float
    target_agent: str
    routing_rationale: str
    requires_approval: bool
    risk_score: float

    # ---- retrieval ------------------------------------------------------
    retrieved: list[dict[str, Any]]
    citations: Annotated[list[Citation], operator.add]

    # ---- tool execution -------------------------------------------------
    tool_traces: Annotated[list[ToolTrace], operator.add]
    tool_call_count: int

    # ---- approval gate ---------------------------------------------------
    approval_id: str | None
    approval_status: str | None
    staged_action: dict[str, Any] | None

    # ---- output ----------------------------------------------------------
    final_answer: str
    guardrail_flags: Annotated[list[str], operator.add]
    error: str | None

    # ---- accounting -------------------------------------------------------
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    model_used: str
    step_count: int


def new_state(**overrides: Any) -> AgentState:
    base: AgentState = {
        "messages": [],
        "retrieved": [],
        "citations": [],
        "tool_traces": [],
        "guardrail_flags": [],
        "tool_call_count": 0,
        "step_count": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cost_usd": 0.0,
        "requires_approval": False,
        "risk_score": 0.0,
        "intent_confidence": 0.0,
    }
    base.update(overrides)  # type: ignore[typeddict-item]
    return base
