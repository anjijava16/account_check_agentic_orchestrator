"""Per-call cost ledger backing the Cost Tracker component."""
from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import Date, Index, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class CostRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "cost_records"
    __table_args__ = (
        Index("ix_cost_tenant_day", "tenant_id", "usage_date"),
        Index("ix_cost_session", "session_id"),
        {"schema": "banking"},
    )

    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str | None] = mapped_column(String(128), index=True)
    session_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    request_id: Mapped[str | None] = mapped_column(String(64), index=True)

    usage_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    call_type: Mapped[str] = mapped_column(String(24), nullable=False)  # chat|embedding|rerank
    agent: Mapped[str | None] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    provider: Mapped[str | None] = mapped_column(String(64))
    deployment: Mapped[str | None] = mapped_column(String(64))  # self_hosted | third_party

    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cached_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(Numeric(12, 6), nullable=False, default=0)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    success: Mapped[bool] = mapped_column(default=True)
    cost_metadata: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)


class DailyBudget(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "daily_budgets"
    __table_args__ = (
        UniqueConstraint("tenant_id", "usage_date", name="uq_budget_tenant_day"),
        {"schema": "banking"},
    )

    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    usage_date: Mapped[date] = mapped_column(Date, nullable=False)
    limit_usd: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    spent_usd: Mapped[float] = mapped_column(Numeric(12, 6), nullable=False, default=0)
    alert_sent: Mapped[bool] = mapped_column(default=False)
