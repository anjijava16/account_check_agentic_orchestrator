"""Specialist agent nodes: Accounts, Transactions, Service.

All three share one implementation because they differ only in prompt and
toolset -- duplicating the ReAct loop three times would just be three places
to fix the same bug.

The loop is explicit rather than a prebuilt agent so we can enforce a hard
tool-call budget, record every step, and interrupt cleanly for approval.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from app.agents.prompts import ACCOUNTS_PROMPT, SERVICE_PROMPT, TRANSACTIONS_PROMPT
from app.agents.state import AgentState
from app.core.config import settings
from app.core.constants import AgentName
from app.core.exceptions import ToolExecutionError
from app.core.logging import get_logger
from app.llm.gateway import get_gateway
from app.llm.model_registry import select_model
from app.mcp.client.manager import get_mcp_manager
from app.observability.metrics import AGENT_INVOCATIONS
from app.observability.tracing import span
from app.security.auth import Principal

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class SpecialistConfig:
    name: str
    prompt: str
    servers: tuple[str, ...]


SPECIALISTS: dict[str, SpecialistConfig] = {
    AgentName.ACCOUNTS: SpecialistConfig(
        name="accounts", prompt=ACCOUNTS_PROMPT, servers=("accounts",)
    ),
    AgentName.TRANSACTIONS: SpecialistConfig(
        name="transactions", prompt=TRANSACTIONS_PROMPT, servers=("transactions", "accounts")
    ),
    AgentName.SERVICE: SpecialistConfig(
        name="service", prompt=SERVICE_PROMPT, servers=("service", "accounts")
    ),
}


def _principal(state: AgentState) -> Principal:
    return Principal(
        subject=state.get("user_id", ""),
        tenant_id=state.get("tenant_id", "default"),
        roles=state.get("roles", ["customer"]),
        customer_ids=[state["customer_id"]] if state.get("customer_id") else [],
    )


def _context_header(state: AgentState) -> str:
    today = datetime.now(UTC).date()
    return (
        f"Today is {today:%A, %d %B %Y}. "
        f"The verified customer identifier for this session is "
        f"{state.get('customer_id') or 'unavailable'}. "
        "Pass it to every tool that requires customer_id."
    )


async def run_specialist(state: AgentState, config: SpecialistConfig) -> dict:
    gateway = get_gateway()
    manager = get_mcp_manager()
    principal = _principal(state)

    tools = manager.tools_for(list(config.servers), principal=principal, agent=config.name)
    tool_map = {t.name: t for t in tools}
    tool_schemas = [_to_openai_schema(t) for t in tools]

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": config.prompt},
        {"role": "system", "content": _context_header(state)},
    ]
    for msg in state.get("messages", [])[-6:]:
        role = "assistant" if isinstance(msg, AIMessage) else "user"
        if isinstance(msg, (HumanMessage, AIMessage)) and msg.content:
            messages.append({"role": role, "content": str(msg.content)})
    messages.append(
        {"role": "user", "content": state.get("sanitised_input") or state["user_input"]}
    )

    model = select_model(classification="confidential")
    traces: list[dict[str, Any]] = []
    citations: list[dict[str, Any]] = []
    prompt_tokens = completion_tokens = 0
    cost = 0.0
    staged_action: dict[str, Any] | None = None
    calls_made = 0

    with span("agent.specialist", **{"agent.name": config.name, "agent.model": model}):
        for iteration in range(settings.agent_max_tool_calls):
            response = await gateway.complete(
                messages,
                model=model,
                tools=tool_schemas or None,
                metadata={
                    "agent": config.name,
                    "session_id": state.get("session_id"),
                    "iteration": iteration,
                },
            )
            if response.usage:
                prompt_tokens += response.usage.prompt_tokens
                completion_tokens += response.usage.completion_tokens
                cost += response.usage.cost_usd

            if not response.tool_calls:
                AGENT_INVOCATIONS.labels(agent=config.name, status="ok").inc()
                return {
                    "final_answer": response.content,
                    "messages": [AIMessage(content=response.content)],
                    "tool_traces": traces,
                    "citations": citations,
                    "prompt_tokens": state.get("prompt_tokens", 0) + prompt_tokens,
                    "completion_tokens": state.get("completion_tokens", 0) + completion_tokens,
                    "cost_usd": round(state.get("cost_usd", 0.0) + cost, 6),
                    "model_used": model,
                    "tool_call_count": state.get("tool_call_count", 0) + calls_made,
                    "staged_action": staged_action,
                    "step_count": state.get("step_count", 0) + 1,
                }

            messages.append(
                {
                    "role": "assistant",
                    "content": response.content or None,
                    "tool_calls": [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {"name": tc["name"], "arguments": tc["arguments"]},
                        }
                        for tc in response.tool_calls
                    ],
                }
            )

            for call in response.tool_calls:
                calls_made += 1
                name = call["name"]
                try:
                    args = json.loads(call["arguments"]) if call["arguments"] else {}
                except json.JSONDecodeError:
                    args = {}

                tool = tool_map.get(name)
                if tool is None:
                    output = json.dumps({"error": "unknown_tool", "tool": name})
                    status = "unknown"
                else:
                    try:
                        output = await tool.ainvoke(args)
                        status = "ok"
                    except ToolExecutionError as exc:
                        output = json.dumps({"error": str(exc)})
                        status = "error"
                    except Exception as exc:  # noqa: BLE001
                        output = json.dumps({"error": f"unexpected: {exc}"})
                        status = "error"

                traces.append(
                    {
                        "server": name.split("__", 1)[0],
                        "tool": name,
                        "arguments": {k: v for k, v in args.items() if k != "customer_id"},
                        "status": status,
                    }
                )
                citations.extend(_extract_citations(output))
                staged_action = _extract_staged_action(name, output) or staged_action

                messages.append(
                    {"role": "tool", "tool_call_id": call["id"], "name": name, "content": output}
                )

    AGENT_INVOCATIONS.labels(agent=config.name, status="budget_exhausted").inc()
    log.warning("agent.tool_budget_exhausted", agent=config.name, calls=calls_made)
    return {
        "final_answer": (
            "I wasn't able to complete that lookup. Let me connect you with a colleague "
            "who can help."
        ),
        "tool_traces": traces,
        "citations": citations,
        "error": "tool_budget_exhausted",
        "tool_call_count": state.get("tool_call_count", 0) + calls_made,
        "prompt_tokens": state.get("prompt_tokens", 0) + prompt_tokens,
        "completion_tokens": state.get("completion_tokens", 0) + completion_tokens,
        "cost_usd": round(state.get("cost_usd", 0.0) + cost, 6),
        "step_count": state.get("step_count", 0) + 1,
    }


def _to_openai_schema(tool: Any) -> dict[str, Any]:
    schema = {}
    if getattr(tool, "args_schema", None) is not None:
        try:
            schema = tool.args_schema.model_json_schema()
        except Exception:  # noqa: BLE001
            schema = {}
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": (tool.description or "")[:1024],
            "parameters": schema or {"type": "object", "properties": {}},
        },
    }


def _extract_citations(output: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(output)
    except (json.JSONDecodeError, TypeError):
        return []
    passages = payload.get("passages") if isinstance(payload, dict) else None
    if not isinstance(passages, list):
        return []
    return [p["citation"] for p in passages if isinstance(p, dict) and "citation" in p]


def _extract_staged_action(tool_name: str, output: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(output)
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(payload, dict) and payload.get("requires_approval"):
        return {"tool": tool_name, **payload}
    return None


async def accounts_node(state: AgentState) -> dict:
    return await run_specialist(state, SPECIALISTS[AgentName.ACCOUNTS])


async def transactions_node(state: AgentState) -> dict:
    return await run_specialist(state, SPECIALISTS[AgentName.TRANSACTIONS])


async def service_node(state: AgentState) -> dict:
    return await run_specialist(state, SPECIALISTS[AgentName.SERVICE])
