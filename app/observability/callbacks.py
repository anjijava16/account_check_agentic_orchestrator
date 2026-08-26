"""LangGraph/LangChain callback handler bridging agent events into telemetry.

Emits: metrics, structured logs, OTel span attributes, and durable AgentStep /
ToolInvocation rows for the observability panel.
"""
from __future__ import annotations

import time
import uuid
from typing import Any
from uuid import UUID

from langchain_core.callbacks import AsyncCallbackHandler

from app.core.logging import get_logger
from app.db.repositories.traces import TraceRepository
from app.db.session import session_scope
from app.observability.metrics import (
    AGENT_INVOCATIONS,
    AGENT_LATENCY,
    PROMPT_SIZE,
    TOOL_CALLS,
    TOOL_LATENCY,
)
from app.observability.tracing import current_span_id, current_trace_id

log = get_logger(__name__)


class TelemetryCallbackHandler(AsyncCallbackHandler):
    def __init__(self, *, session_id: uuid.UUID, turn_id: uuid.UUID, agent: str = "coordinator"):
        self.session_id = session_id
        self.turn_id = turn_id
        self.agent = agent
        self._starts: dict[str, float] = {}
        self._step_index = 0
        self.persist_errors = 0

    # ------------------------------------------------------------------ LLM
    async def on_llm_start(
        self, serialized: dict[str, Any], prompts: list[str], *, run_id: UUID, **kwargs: Any
    ) -> None:
        self._starts[str(run_id)] = time.perf_counter()
        approx_tokens = sum(len(p) for p in prompts) // 4
        PROMPT_SIZE.labels(agent=self.agent).observe(approx_tokens)

    async def on_llm_end(self, response: Any, *, run_id: UUID, **kwargs: Any) -> None:
        elapsed = self._elapsed(run_id)
        AGENT_LATENCY.labels(agent=self.agent).observe(elapsed)
        AGENT_INVOCATIONS.labels(agent=self.agent, status="ok").inc()
        await self._persist_step(
            node="llm", status="ok", duration_ms=int(elapsed * 1000),
            output_preview=str(response)[:1000],
        )

    async def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        AGENT_INVOCATIONS.labels(agent=self.agent, status="error").inc()
        await self._persist_step(
            node="llm", status="error", duration_ms=int(self._elapsed(run_id) * 1000),
            output_preview=str(error)[:1000],
        )

    # ----------------------------------------------------------------- tools
    async def on_tool_start(
        self, serialized: dict[str, Any], input_str: str, *, run_id: UUID, **kwargs: Any
    ) -> None:
        self._starts[str(run_id)] = time.perf_counter()
        log.info("tool.start", tool=serialized.get("name"), agent=self.agent)

    async def on_tool_end(self, output: Any, *, run_id: UUID, **kwargs: Any) -> None:
        name = kwargs.get("name", "unknown")
        server = name.split("__", 1)[0] if "__" in name else "unknown"
        elapsed = self._elapsed(run_id)
        TOOL_CALLS.labels(server=server, tool=name, status="ok").inc()
        TOOL_LATENCY.labels(server=server, tool=name).observe(elapsed)
        await self._persist_tool(
            server, name, status="ok", duration_ms=int(elapsed * 1000),
            result_preview=str(output)[:2000],
        )

    async def on_tool_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        name = kwargs.get("name", "unknown")
        server = name.split("__", 1)[0] if "__" in name else "unknown"
        TOOL_CALLS.labels(server=server, tool=name, status="error").inc()
        await self._persist_tool(
            server, name, status="error", duration_ms=int(self._elapsed(run_id) * 1000),
            error=str(error),
        )

    # --------------------------------------------------------------- helpers
    def _elapsed(self, run_id: UUID) -> float:
        started = self._starts.pop(str(run_id), None)
        return time.perf_counter() - started if started else 0.0

    async def _persist_step(self, **fields: Any) -> None:
        self._step_index += 1
        try:
            async with session_scope() as session:
                await TraceRepository(session).add_step(
                    session_id=self.session_id,
                    turn_id=self.turn_id,
                    step_index=self._step_index,
                    agent=self.agent,
                    trace_id=current_trace_id(),
                    span_id=current_span_id(),
                    **fields,
                )
        except Exception as exc:  # noqa: BLE001
            self.persist_errors += 1
            log.debug("telemetry.step_persist_failed", error=str(exc))

    async def _persist_tool(self, server: str, tool: str, **fields: Any) -> None:
        try:
            async with session_scope() as session:
                await TraceRepository(session).add_tool_invocation(
                    session_id=self.session_id,
                    turn_id=self.turn_id,
                    agent=self.agent,
                    mcp_server=server,
                    tool_name=tool,
                    trace_id=current_trace_id(),
                    **fields,
                )
        except Exception as exc:  # noqa: BLE001
            self.persist_errors += 1
            log.debug("telemetry.tool_persist_failed", error=str(exc))
