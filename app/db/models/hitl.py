"""Human-in-the-loop approval gate for high-risk service actions."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ApprovalRequest(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "approval_requests"
    __table_args__ = (
        Index("ix_approvals_status_created", "status", "created_at"),
        {"schema": "banking"},
    )

    session_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), index=True)
    thread_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    checkpoint_id: Mapped[str | None] = mapped_column(String(128))
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    requested_by: Mapped[str] = mapped_column(String(128), nullable=False)

    intent: Mapped[str] = mapped_column(String(48), nullable=False)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    risk_score: Mapped[float | None] = mapped_column()
    rationale: Mapped[str | None] = mapped_column(Text)

    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    decided_by: Mapped[str | None] = mapped_column(String(128))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
