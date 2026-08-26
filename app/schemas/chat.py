"""Chat API contracts."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000, description="Customer's message")
    session_id: uuid.UUID | None = Field(
        default=None, description="Omit to start a new conversation"
    )
    stream: bool = Field(default=False, description="Return an SSE token stream")
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("message")
    @classmethod
    def _strip(cls, v: str) -> str:
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("message cannot be blank")
        return cleaned


class CitationOut(BaseModel):
    chunk_id: str | None = None
    document_id: str | None = None
    filename: str | None = None
    page: int | None = None
    section: str | None = None
    score: float | None = None


class ToolTraceOut(BaseModel):
    server: str | None = None
    tool: str | None = None
    status: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)


class ApprovalPrompt(BaseModel):
    approval_id: str
    reference: str | None = None
    intent: str | None = None
    action: str | None = None
    changes: dict[str, Any] = Field(default_factory=dict)
    risk_score: float = 0.0


class UsageOut(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    model: str | None = None
    latency_ms: int = 0


class ChatResponse(BaseModel):
    session_id: uuid.UUID
    turn_id: uuid.UUID
    answer: str
    status: Literal["completed", "pending_approval", "error"] = "completed"
    intent: str | None = None
    agent: str | None = None
    citations: list[CitationOut] = Field(default_factory=list)
    tools_used: list[ToolTraceOut] = Field(default_factory=list)
    approval: ApprovalPrompt | None = None
    usage: UsageOut = Field(default_factory=UsageOut)
    trace_id: str | None = None


class MessageOut(BaseModel):
    role: str
    content: str
    agent: str | None = None
    intent: str | None = None
    citations: list[dict[str, Any]] = Field(default_factory=list)
    created_at: str | None = None


class SessionOut(BaseModel):
    session_id: uuid.UUID
    title: str | None = None
    status: str
    channel: str
    message_count: int
    total_tokens: int
    total_cost_usd: float
    last_activity_at: datetime | None = None
    created_at: datetime


class HistoryResponse(BaseModel):
    session_id: uuid.UUID
    messages: list[MessageOut]


class ApprovalDecisionRequest(BaseModel):
    decision: Literal["approved", "rejected"]
    note: str | None = Field(default=None, max_length=1000)


class ApprovalOut(BaseModel):
    approval_id: uuid.UUID
    session_id: uuid.UUID
    intent: str
    action: str
    payload: dict[str, Any]
    status: str
    risk_score: float | None = None
    requested_by: str
    created_at: datetime
    expires_at: datetime | None = None
