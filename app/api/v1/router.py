"""API v1 aggregate router.

Routers are mounted in the order a request would naturally traverse them:
chat first (the product), then ingestion (what feeds it), then the operational
surfaces.
"""
from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import admin, chat, evaluation, ingestion

api_router = APIRouter()
api_router.include_router(chat.router)
api_router.include_router(ingestion.router)
api_router.include_router(evaluation.router)
api_router.include_router(admin.router)
