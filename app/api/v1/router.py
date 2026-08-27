"""API v1 aggregate router.

Routers are mounted in the order a request would naturally traverse them:
chat first (the product), then ingestion (what feeds it), then the operational
surfaces, then the per-service CRUD surfaces.
"""
from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import admin, chat, evaluation, ingestion
from app.api.v1.services import (
    agents,
    docling,
    litellm,
    llm,
    mcp,
    observability,
    opensearch,
    postgres,
    redis,
    security,
    storage,
    tiktoken,
    traces,
)

api_router = APIRouter()
api_router.include_router(chat.router)
api_router.include_router(ingestion.router)
api_router.include_router(evaluation.router)
api_router.include_router(admin.router)

# Per-service CRUD surfaces (operator-only) under /services/*.
api_router.include_router(postgres.router)
api_router.include_router(redis.router)
api_router.include_router(opensearch.router)
api_router.include_router(storage.router)
api_router.include_router(traces.router)
api_router.include_router(mcp.router)
api_router.include_router(llm.router)
api_router.include_router(litellm.router)
api_router.include_router(security.router)
api_router.include_router(tiktoken.router)
api_router.include_router(agents.router)
api_router.include_router(observability.router)
api_router.include_router(docling.router)
