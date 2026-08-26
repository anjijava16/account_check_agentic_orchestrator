"""Repository for durable chat sessions and messages."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import MessageRole
from app.db.models import ChatMessage, ChatSession


class ChatRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_or_create_session(
        self, *, session_id: uuid.UUID | None, tenant_id: str, user_id: str, channel: str = "web"
    ) -> ChatSession:
        if session_id:
            stmt = select(ChatSession).where(
                ChatSession.id == session_id,
                ChatSession.tenant_id == tenant_id,
                ChatSession.user_id == user_id,
            )
            existing = (await self.session.execute(stmt)).scalar_one_or_none()
            if existing:
                return existing
        chat = ChatSession(
            id=session_id or uuid.uuid4(),
            tenant_id=tenant_id,
            user_id=user_id,
            channel=channel,
            last_activity_at=datetime.now(UTC),
        )
        self.session.add(chat)
        await self.session.flush()
        return chat

    async def next_sequence(self, session_id: uuid.UUID) -> int:
        stmt = select(func.coalesce(func.max(ChatMessage.sequence), 0)).where(
            ChatMessage.session_id == session_id
        )
        return int((await self.session.execute(stmt)).scalar_one()) + 1

    async def add_message(
        self,
        *,
        session_id: uuid.UUID,
        role: MessageRole | str,
        content: str,
        sequence: int | None = None,
        **fields: Any,
    ) -> ChatMessage:
        seq = sequence if sequence is not None else await self.next_sequence(session_id)
        msg = ChatMessage(
            session_id=session_id, role=str(role), content=content, sequence=seq, **fields
        )
        self.session.add(msg)
        await self.session.execute(
            update(ChatSession)
            .where(ChatSession.id == session_id)
            .values(
                message_count=ChatSession.message_count + 1,
                last_activity_at=datetime.now(UTC),
                total_tokens=ChatSession.total_tokens
                + fields.get("prompt_tokens", 0)
                + fields.get("completion_tokens", 0),
            )
        )
        await self.session.flush()
        return msg

    async def history(self, session_id: uuid.UUID, limit: int = 50) -> list[ChatMessage]:
        stmt = (
            select(ChatMessage)
            .where(ChatMessage.session_id == session_id)
            .order_by(ChatMessage.sequence.desc())
            .limit(limit)
        )
        rows = list((await self.session.execute(stmt)).scalars().all())
        return list(reversed(rows))

    async def list_sessions(
        self, tenant_id: str, user_id: str, limit: int = 30, offset: int = 0
    ) -> list[ChatSession]:
        stmt = (
            select(ChatSession)
            .where(ChatSession.tenant_id == tenant_id, ChatSession.user_id == user_id)
            .order_by(ChatSession.last_activity_at.desc().nullslast())
            .limit(limit)
            .offset(offset)
        )
        return list((await self.session.execute(stmt)).scalars().all())
