"""LLM test bench.

Operator-only endpoints for exercising the model gateway directly: run a
completion, run an embedding, and inspect the model registry. These make real
model calls and therefore cost money -- hence the operator gate.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.deps import require_role
from app.core.exceptions import LLMError
from app.core.logging import get_logger
from app.llm.gateway import get_gateway
from app.llm.model_registry import MODEL_REGISTRY

log = get_logger(__name__)
router = APIRouter(
    prefix="/services/llm",
    tags=["services: llm"],
    dependencies=[Depends(require_role("agent_operator"))],
)


class CompletionRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=8000)
    system: str | None = Field(default=None, max_length=4000)
    model: str | None = None
    temperature: float = Field(default=0.1, ge=0.0, le=2.0)
    max_tokens: int = Field(default=256, ge=1, le=4096)


class EmbeddingRequest(BaseModel):
    texts: list[str] = Field(min_length=1, max_length=32)
    model: str | None = None


@router.get("/models", summary="List registered models")
async def list_models() -> dict[str, Any]:
    return {
        "count": len(MODEL_REGISTRY),
        "models": [
            {
                "name": spec.name,
                "litellm_model": spec.litellm_model,
                "deployment": spec.deployment,
                "provider": spec.provider,
                "context_window": spec.context_window,
                "supports_tools": spec.supports_tools,
                "classification_max": spec.data_classification_max,
            }
            for spec in MODEL_REGISTRY.values()
        ],
    }


@router.post("/complete", summary="Run a completion")
async def complete(body: CompletionRequest) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    if body.system:
        messages.append({"role": "system", "content": body.system})
    messages.append({"role": "user", "content": body.prompt})
    try:
        response = await get_gateway().complete(
            messages,
            model=body.model,
            temperature=body.temperature,
            max_tokens=body.max_tokens,
        )
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    usage = response.usage
    return {
        "content": response.content,
        "finish_reason": response.finish_reason,
        "usage": {
            "model": usage.model if usage else None,
            "prompt_tokens": usage.prompt_tokens if usage else None,
            "completion_tokens": usage.completion_tokens if usage else None,
            "cost_usd": usage.cost_usd if usage else None,
            "latency_ms": usage.latency_ms if usage else None,
            "fallback_used": usage.fallback_used if usage else None,
        },
    }


@router.post("/embed", summary="Embed texts")
async def embed(body: EmbeddingRequest) -> dict[str, Any]:
    try:
        vectors, usage = await get_gateway().embed(body.texts, model=body.model)
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        "count": len(vectors),
        "dimensions": len(vectors[0]) if vectors else 0,
        "model": usage.model,
        "prompt_tokens": usage.prompt_tokens,
        "vectors_preview": [v[:8] for v in vectors],
    }
