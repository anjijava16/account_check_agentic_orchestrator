"""Shared response envelopes."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ErrorDetail(BaseModel):
    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)
    request_id: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail


class ComponentHealth(BaseModel):
    name: str
    status: Literal["up", "down", "degraded"]
    latency_ms: float | None = None
    detail: str | None = None


class HealthResponse(BaseModel):
    status: Literal["healthy", "degraded", "unhealthy"]
    version: str
    environment: str
    components: list[ComponentHealth] = Field(default_factory=list)


class AcceptedResponse(BaseModel):
    accepted: bool = True
    message: str
    reference: str | None = None
