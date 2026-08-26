"""Repository for human-in-the-loop approvals."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError
from app.db.models import ApprovalRequest


class ApprovalRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, **fields: Any) -> ApprovalRequest:
        req = ApprovalRequest(**fields)
        self.session.add(req)
        await self.session.flush()
        return req

    async def get(self, approval_id: uuid.UUID) -> ApprovalRequest:
        req = (
            await self.session.execute(
                select(ApprovalRequest).where(ApprovalRequest.id == approval_id)
            )
        ).scalar_one_or_none()
        if req is None:
            raise NotFoundError("Approval request not found", details={"id": str(approval_id)})
        return req

    async def pending(self, tenant_id: str, limit: int = 50) -> list[ApprovalRequest]:
        stmt = (
            select(ApprovalRequest)
            .where(ApprovalRequest.tenant_id == tenant_id, ApprovalRequest.status == "pending")
            .order_by(ApprovalRequest.created_at)
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def decide(
        self, approval_id: uuid.UUID, *, decision: str, decided_by: str, note: str | None = None
    ) -> ApprovalRequest:
        req = await self.get(approval_id)
        if req.status != "pending":
            raise ConflictError(
                "Approval already decided", details={"status": req.status}
            )
        req.status = decision
        req.decided_by = decided_by
        req.decided_at = datetime.now(UTC)
        req.decision_note = note
        await self.session.flush()
        return req
