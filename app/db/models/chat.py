"""Durable conversation record. Redis holds the hot copy, Postgres the archive."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ChatSession(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "chat_sessions"
    __table_args__ = (
        Index("ix_sessions_user_active", "user_id", "last_activity_at"),
        {"schema": "banking"},
    )

    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    channel: Mapped[str] = mapped_column(String(32), nullable=False, default="web")
    title: Mapped[str | None] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="active")

    message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_cost_usd: Mapped[float] = mapped_column(Numeric(12, 6), nullable=False, default=0)
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    session_metadata: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    messages: Mapped[list[ChatMessage]] = relationship(
        back_populates="session", cascade="all, delete-orphan", lazy="noload"
    )


class ChatMessage(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "chat_messages"
    __table_args__ = (
        Index("ix_messages_session_seq", "session_id", "sequence"),
        {"schema": "banking"},
    )

    session_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("banking.chat_sessions.id", ondelete="CASCADE")
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    redacted_content: Mapped[str | None] = mapped_column(Text)

    agent: Mapped[str | None] = mapped_column(String(32))
    intent: Mapped[str | None] = mapped_column(String(48))
    model: Mapped[str | None] = mapped_column(String(128))
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    citations: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    trace_id: Mapped[str | None] = mapped_column(String(64), index=True)
    message_metadata: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    session: Mapped[ChatSession] = relationship(back_populates="messages")
