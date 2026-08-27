"""MCP server introspection + tool invocation.

MCP servers are external processes, so this surface is Read + Execute rather than
full CRUD: list servers/health/config, list tools with their schemas, invoke a
tool through the policy-wrapped path, and force a reconnect.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import PrincipalDep, require_role
from app.core.logging import get_logger
from app.mcp.client.manager import SERVER_CONFIG, get_mcp_manager

log = get_logger(__name__)
router = APIRouter(
    prefix="/services/mcp",
    tags=["services: mcp"],
    dependencies=[Depends(require_role("agent_operator"))],
)


class ToolInvocation(BaseModel):
    arguments: dict[str, Any] = Field(default_factory=dict)


def _tool_schema(tool: Any) -> dict[str, Any]:
    schema = None
    if getattr(tool, "args_schema", None) is not None:
        try:
            schema = tool.args_schema.model_json_schema()
        except Exception:  # noqa: BLE001
            schema = None
    return {"name": tool.name, "description": tool.description, "args_schema": schema}


@router.get("/servers", summary="List MCP servers with health + config")
async def list_servers() -> dict[str, Any]:
    manager = get_mcp_manager()
    health = manager.health
    return {
        "count": len(SERVER_CONFIG),
        "servers": [
            {
                "name": name,
                "url": cfg["url"],
                "transport": cfg["transport"],
                "healthy": health.get(name, False),
                "tool_count": len(manager.raw_tools([name])),
            }
            for name, cfg in SERVER_CONFIG.items()
        ],
    }


@router.get("/tools", summary="List all tools across servers")
async def list_all_tools() -> dict[str, Any]:
    manager = get_mcp_manager()
    tools = [_tool_schema(t) for t in manager.raw_tools()]
    return {"count": len(tools), "tools": tools}


@router.get("/servers/{server}/tools", summary="List tools for one server")
async def list_server_tools(server: str) -> dict[str, Any]:
    if server not in SERVER_CONFIG:
        raise HTTPException(status_code=404, detail=f"Unknown MCP server: {server}")
    tools = [_tool_schema(t) for t in get_mcp_manager().raw_tools([server])]
    return {"server": server, "count": len(tools), "tools": tools}


@router.post("/servers/{server}/tools/{tool}/invoke", summary="Invoke a tool")
async def invoke_tool(
    server: str, tool: str, body: ToolInvocation, principal: PrincipalDep
) -> dict[str, Any]:
    if server not in SERVER_CONFIG:
        raise HTTPException(status_code=404, detail=f"Unknown MCP server: {server}")
    manager = get_mcp_manager()
    # Policy-wrapped tools inject identity args and enforce authorization.
    wrapped = {
        t.name: t
        for t in manager.tools_for([server], principal=principal, agent="mcp-console")
    }
    namespaced = f"{server}__{tool}"
    target = wrapped.get(namespaced)
    if target is None:
        raise HTTPException(status_code=404, detail=f"Unknown tool: {namespaced}")
    try:
        result = await target.ainvoke(body.arguments)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Tool invocation failed: {exc}") from exc
    return {"server": server, "tool": namespaced, "result": result}


@router.post("/reconnect", status_code=status.HTTP_202_ACCEPTED, summary="Reconnect servers")
async def reconnect() -> dict[str, Any]:
    manager = get_mcp_manager()
    await manager.aclose()
    await manager.connect()
    return {"reconnected": True, "health": manager.health}
