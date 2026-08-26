"""Agent-step and tool-invocation records powering the Observability panel."""
from __future__ import annotations

import uuid

from sqlalchemy import Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class AgentStep(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "agent_steps"
    __table_args__ = (
        Index("ix_steps_session_seq", "session_id", "step_index"),
        {"schema": "banking"},
    )

    session_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), index=True)
    turn_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), index=True)
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    node: Mapped[str] = mapped_column(String(64), nullable=False)
    agent: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="ok")
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    prompt_preview: Mapped[str | None] = mapped_column(Text)
    output_preview: Mapped[str | None] = mapped_column(Text)
    trace_id: Mapped[str | None] = mapped_column(String(64), index=True)
    span_id: Mapped[str | None] = mapped_column(String(64))
    step_metadata: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)


class ToolInvocation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "tool_invocations"
    __table_args__ = (
        Index("ix_tools_name_created", "tool_name", "created_at"),
        {"schema": "banking"},
    )

    session_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), index=True)
    turn_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    agent: Mapped[str | None] = mapped_column(String(32))
    mcp_server: Mapped[str] = mapped_column(String(64), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    arguments: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    result_preview: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="ok")
    error: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    trace_id: Mapped[str | None] = mapped_column(String(64), index=True)
