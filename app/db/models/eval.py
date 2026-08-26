"""Agent Evaluation Suite persistence."""
from __future__ import annotations

import uuid

from sqlalchemy import Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class EvalRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "eval_runs"
    __table_args__ = ({"schema": "banking"},)

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    mode: Mapped[str] = mapped_column(String(24), nullable=False, default="offline")  # offline|online
    dataset: Mapped[str | None] = mapped_column(String(255))
    git_sha: Mapped[str | None] = mapped_column(String(64))
    model: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="running")
    total_cases: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    passed_cases: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    aggregate_scores: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    results: Mapped[list[EvalResult]] = relationship(
        back_populates="run", cascade="all, delete-orphan", lazy="noload"
    )


class EvalResult(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "eval_results"
    __table_args__ = (Index("ix_eval_results_run", "run_id"), {"schema": "banking"})

    run_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("banking.eval_runs.id", ondelete="CASCADE")
    )
    case_id: Mapped[str] = mapped_column(String(128), nullable=False)
    metric: Mapped[str] = mapped_column(String(64), nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    passed: Mapped[bool] = mapped_column(default=True)
    question: Mapped[str | None] = mapped_column(Text)
    prediction: Mapped[str | None] = mapped_column(Text)
    reference: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    run: Mapped[EvalRun] = relationship(back_populates="results")
