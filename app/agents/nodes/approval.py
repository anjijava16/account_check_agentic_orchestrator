"""Human-in-the-loop approval node.

When a specialist stages a high-risk action, this node persists an
ApprovalRequest and then calls LangGraph's `interrupt()`. The graph stops, the
checkpointer writes the full state to Postgres, and the API returns a
`pending_approval` response.

Hours later an operator decides, the API resumes the same thread_id with a
Command(resume=...), and execution continues from exactly this node with the
staged action intact. That durability is the whole reason for the Postgres
checkpointer.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from langgraph.types import interrupt

from app.agents.state import AgentState
from app.core.logging import get_logger
from app.db.repositories.approvals import ApprovalRepository
from app.db.session import session_scope
from app.observability.metrics import HITL_INTERRUPTS

log = get_logger(__name__)

APPROVAL_TTL_HOURS = 48


async def approval_node(state: AgentState) -> dict:
    staged = state.get("staged_action")
    if not staged:
        return {"approval_status": "not_required"}

    reference = staged.get("reference") or f"REF-{uuid.uuid4().hex[:8].upper()}"

    async with session_scope() as session:
        request = await ApprovalRepository(session).create(
            session_id=uuid.UUID(state["session_id"]),
            thread_id=state["session_id"],
            tenant_id=state.get("tenant_id", "default"),
            requested_by=state.get("user_id", "unknown"),
            intent=state.get("intent", "unknown"),
            action=staged.get("tool", "unknown"),
            payload={k: v for k, v in staged.items() if k != "tool"},
            risk_score=state.get("risk_score", 0.0),
            rationale=state.get("routing_rationale"),
            expires_at=datetime.now(UTC) + timedelta(hours=APPROVAL_TTL_HOURS),
        )
        approval_id = str(request.id)

    HITL_INTERRUPTS.labels(intent=state.get("intent", "unknown")).inc()
    log.info(
        "hitl.interrupt",
        approval_id=approval_id,
        intent=state.get("intent"),
        action=staged.get("tool"),
        session_id=state.get("session_id"),
    )

    # Execution stops here. The payload is surfaced to the operator UI.
    decision = interrupt(
        {
            "type": "approval_required",
            "approval_id": approval_id,
            "reference": reference,
            "intent": state.get("intent"),
            "action": staged.get("tool"),
            "changes": staged.get("changes") or staged.get("payload") or {},
            "risk_score": state.get("risk_score", 0.0),
            "requested_by": state.get("user_id"),
        }
    )

    # --- resumed from here after a human decides -----------------------
    status = (decision or {}).get("decision", "rejected")
    decided_by = (decision or {}).get("decided_by", "unknown")
    log.info("hitl.resumed", approval_id=approval_id, decision=status, decided_by=decided_by)

    if status != "approved":
        note = (decision or {}).get("note")
        answer = (
            f"That request ({reference}) wasn't approved"
            + (f": {note}." if note else ". A colleague will be in touch.")
        )
        return {
            "approval_id": approval_id,
            "approval_status": status,
            "final_answer": answer,
        }

    applied = await _commit(reference, decided_by)
    return {
        "approval_id": approval_id,
        "approval_status": "approved",
        "final_answer": (
            f"That's now approved and applied. Your reference is {reference}."
            if applied
            else f"That's approved. Your reference is {reference} and it's being processed."
        ),
    }


async def _commit(reference: str, approved_by: str) -> bool:
    """Call the service MCP server's commit tool with the operator's identity."""
    from app.mcp.client.manager import get_mcp_manager

    manager = get_mcp_manager()
    for tool in manager.raw_tools(["service"]):
        if tool.name == "confirm_service_request":
            try:
                await tool.ainvoke({"reference": reference, "approved_by": approved_by})
                return True
            except Exception as exc:  # noqa: BLE001
                log.error("hitl.commit_failed", reference=reference, error=str(exc))
                return False
    log.warning("hitl.commit_tool_missing", reference=reference)
    return False
