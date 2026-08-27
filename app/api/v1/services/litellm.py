"""LiteLLM proxy passthrough.

Talks to the LiteLLM container (the platform's model egress) using the master
key: list models, check health, and run a chat completion through the proxy.
Distinct from /services/llm, which uses the in-process router.
"""
from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.deps import require_role
from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)
router = APIRouter(
    prefix="/services/litellm",
    tags=["services: litellm"],
    dependencies=[Depends(require_role("agent_operator"))],
)


class ProxyChatRequest(BaseModel):
    model: str = Field(default_factory=lambda: settings.primary_model)
    messages: list[dict[str, Any]] = Field(
        default_factory=lambda: [{"role": "user", "content": "ping"}]
    )
    temperature: float = Field(default=0.1, ge=0.0, le=2.0)
    max_tokens: int = Field(default=128, ge=1, le=4096)


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.litellm_master_key}"}


async def _proxy_request(method: str, path: str, json: dict[str, Any] | None = None) -> Any:
    url = f"{settings.litellm_base_url.rstrip('/')}{path}"
    try:
        async with httpx.AsyncClient(timeout=settings.llm_timeout_seconds) as client:
            response = await client.request(method, url, headers=_headers(), json=json)
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=exc.response.status_code, detail=f"LiteLLM error: {exc}"
        ) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"LiteLLM unreachable: {exc}") from exc


@router.get("/models", summary="List proxy models")
async def list_models() -> dict[str, Any]:
    data = await _proxy_request("GET", "/v1/models")
    return data


@router.get("/health", summary="Proxy health")
async def proxy_health() -> dict[str, Any]:
    return await _proxy_request("GET", "/health")


@router.post("/chat", summary="Chat completion via the proxy")
async def proxy_chat(body: ProxyChatRequest) -> dict[str, Any]:
    payload = {
        "model": body.model,
        "messages": body.messages,
        "temperature": body.temperature,
        "max_tokens": body.max_tokens,
    }
    return await _proxy_request("POST", "/v1/chat/completions", json=payload)
