"""MCP client manager.

Owns the connections to the three MCP servers and exposes their tools to the
LangGraph agents as LangChain tools. Key production concerns handled here:

  * one long-lived multi-server client, not a connection per turn
  * namespacing (`accounts__get_balance`) so two servers can expose the same
    tool name without collision
  * an authorisation shim wrapped around every tool: the policy engine sees
    the call before the MCP server does
  * argument injection: `customer_id` comes from the verified principal, so a
    prompt cannot talk the model into passing someone else's id
  * graceful degradation: a dead server removes its tools rather than failing
    the whole turn
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool

from app.core.config import settings
from app.core.exceptions import ToolExecutionError
from app.core.logging import get_logger
from app.observability.metrics import TOOL_CALLS, TOOL_LATENCY
from app.observability.tracing import span
from app.security.auth import Principal
from app.security.authz import enforce_tool

log = get_logger(__name__)

SERVER_CONFIG: dict[str, dict[str, Any]] = {
    "accounts": {"url": settings.mcp_accounts_url, "transport": settings.mcp_transport},
    "transactions": {"url": settings.mcp_transactions_url, "transport": settings.mcp_transport},
    "service": {"url": settings.mcp_service_url, "transport": settings.mcp_transport},
}

# Arguments the platform fills in from the verified principal. The model is
# never trusted to supply these, even if it produces a value for them.
INJECTED_ARGS = {"customer_id", "tenant_id", "approved_by"}


class MCPToolManager:
    def __init__(self) -> None:
        self._client: Any = None
        self._tools_by_server: dict[str, list[BaseTool]] = {}
        self._healthy: dict[str, bool] = {}
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        """Establish the multi-server client and load tool definitions."""
        from langchain_mcp_adapters.client import MultiServerMCPClient

        async with self._lock:
            self._client = MultiServerMCPClient(SERVER_CONFIG)
            for name in SERVER_CONFIG:
                try:
                    tools = await asyncio.wait_for(
                        self._client.get_tools(server_name=name),
                        timeout=settings.mcp_call_timeout_seconds,
                    )
                    self._tools_by_server[name] = tools
                    self._healthy[name] = True
                    log.info(
                        "mcp.server_connected",
                        server=name,
                        tools=[t.name for t in tools],
                    )
                except Exception as exc:  # noqa: BLE001, PERF203
                    self._tools_by_server[name] = []
                    self._healthy[name] = False
                    log.error("mcp.server_unavailable", server=name, error=str(exc))

    async def aclose(self) -> None:
        self._tools_by_server.clear()
        self._client = None

    @property
    def health(self) -> dict[str, bool]:
        return dict(self._healthy)

    def raw_tools(self, servers: list[str] | None = None) -> list[BaseTool]:
        names = servers or list(self._tools_by_server)
        return [t for name in names for t in self._tools_by_server.get(name, [])]

    # ------------------------------------------------------------- wrapping
    def tools_for(
        self,
        servers: list[str],
        *,
        principal: Principal,
        agent: str,
        injected: dict[str, Any] | None = None,
    ) -> list[BaseTool]:
        """Return policy-wrapped, namespaced tools for one agent's toolset."""
        wrapped: list[BaseTool] = []
        base_injections = {
            "customer_id": (principal.customer_ids or [None])[0],
            "tenant_id": principal.tenant_id,
            **(injected or {}),
        }
        for server in servers:
            for tool in self._tools_by_server.get(server, []):
                wrapped.append(
                    self._wrap(tool, server=server, principal=principal, agent=agent,
                               injections=base_injections)
                )
        return wrapped

    def _wrap(
        self,
        tool: BaseTool,
        *,
        server: str,
        principal: Principal,
        agent: str,
        injections: dict[str, Any],
    ) -> BaseTool:
        namespaced = f"{server}__{tool.name}"

        async def _run(**kwargs: Any) -> str:
            import time

            # Overwrite identity-bearing args with verified values.
            for key in INJECTED_ARGS:
                if key in injections and injections[key] is not None:
                    kwargs[key] = injections[key]

            enforce_tool(principal, namespaced, kwargs)

            started = time.perf_counter()
            with span(
                "mcp.tool_call",
                **{"mcp.server": server, "mcp.tool": tool.name, "agent.name": agent},
            ):
                try:
                    result = await asyncio.wait_for(
                        tool.ainvoke(kwargs), timeout=settings.mcp_call_timeout_seconds
                    )
                except TimeoutError as exc:
                    TOOL_CALLS.labels(server=server, tool=namespaced, status="timeout").inc()
                    raise ToolExecutionError(
                        f"{namespaced} timed out after {settings.mcp_call_timeout_seconds}s"
                    ) from exc
                except Exception as exc:  # noqa: BLE001
                    TOOL_CALLS.labels(server=server, tool=namespaced, status="error").inc()
                    log.error("mcp.tool_failed", tool=namespaced, error=str(exc))
                    raise ToolExecutionError(f"{namespaced} failed: {exc}") from exc

            elapsed = time.perf_counter() - started
            TOOL_CALLS.labels(server=server, tool=namespaced, status="ok").inc()
            TOOL_LATENCY.labels(server=server, tool=namespaced).observe(elapsed)
            log.info(
                "mcp.tool_ok", tool=namespaced, agent=agent, duration_ms=int(elapsed * 1000)
            )
            return result if isinstance(result, str) else json.dumps(result, default=str)

        return StructuredTool(
            name=namespaced,
            description=tool.description,
            args_schema=tool.args_schema,
            coroutine=_run,
            handle_tool_error=True,
        )


_manager: MCPToolManager | None = None


def get_mcp_manager() -> MCPToolManager:
    global _manager
    if _manager is None:
        _manager = MCPToolManager()
    return _manager
