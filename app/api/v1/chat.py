"""ChatRouter.

The customer-facing conversational surface. Everything here is thin: validate,
delegate to the orchestrator, shape the response. No business logic.

Endpoints:
  POST   /chat                      -- one conversational turn
  POST   /chat/stream               -- same, as SSE
  GET    /chat/sessions             -- list the caller's conversations
  GET    /chat/sessions/{id}        -- conversation history
  DELETE /chat/sessions/{id}        -- clear the hot session state
  GET    /chat/approvals            -- pending approvals (operators)
  POST   /chat/approvals/{id}       -- approve/reject and resume the graph
"""
from __future__ import annotations

import json
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sse_starlette.sse import EventSourceResponse

from app.agents.orchestrator import get_orchestrator
from app.api.deps import PrincipalDep, per_user_rate_limit, require_role
from app.core.exceptions import NotFoundError
from app.core.logging import get_logger, session_id_ctx
from app.db.repositories.approvals import ApprovalRepository
from app.db.repositories.chat import ChatRepository
from app.db.session import session_scope
from app.schemas.chat import (
    ApprovalDecisionRequest,
    ApprovalOut,
    ChatRequest,
    ChatResponse,
    HistoryResponse,
    MessageOut,
    SessionOut,
    UsageOut,
)

log = get_logger(__name__)
router = APIRouter(prefix="/chat", tags=["chat"])


@router.post(
    "",
    response_model=ChatResponse,
    summary="Send a message",
    dependencies=[Depends(per_user_rate_limit)],
)
async def chat(payload: ChatRequest, principal: PrincipalDep) -> ChatResponse:
    """Run one conversational turn through the agent graph.

    Returns `status="pending_approval"` when the turn staged a high-risk action;
    the conversation is durably paused and resumes via the approvals endpoint.
    """
    if payload.session_id:
        session_id_ctx.set(str(payload.session_id))

    result = await get_orchestrator().handle_turn(
        message=payload.message, principal=principal, session_id=payload.session_id
    )

    return ChatResponse(
        session_id=result.session_id,
        turn_id=result.turn_id,
        answer=result.answer,
        status=result.status,
        intent=result.intent,
        agent=result.agent,
        citations=result.citations,
        tools_used=result.tool_traces,
        approval=result.approval,
        usage=UsageOut(
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            total_tokens=result.prompt_tokens + result.completion_tokens,
            cost_usd=result.cost_usd,
            model=result.model,
            latency_ms=result.latency_ms,
        ),
        trace_id=result.trace_id,
    )


@router.post(
    "/stream",
    summary="Send a message (SSE)",
    dependencies=[Depends(per_user_rate_limit)],
)
async def chat_stream(payload: ChatRequest, principal: PrincipalDep):
    """Server-sent events. Emits progress events as the graph advances so the
    UI can show which specialist is working instead of a blank spinner."""

    async def generator():
        yield {"event": "start", "data": json.dumps({"status": "routing"})}
        try:
            result = await get_orchestrator().handle_turn(
                message=payload.message, principal=principal, session_id=payload.session_id
            )
            yield {
                "event": "routed",
                "data": json.dumps({"intent": result.intent, "agent": result.agent}),
            }
            for trace in result.tool_traces:
                yield {"event": "tool", "data": json.dumps(trace)}
            yield {
                "event": "answer",
                "data": json.dumps(
                    {
                        "session_id": str(result.session_id),
                        "answer": result.answer,
                        "status": result.status,
                        "citations": result.citations,
                    }
                ),
            }
            yield {
                "event": "usage",
                "data": json.dumps(
                    {
                        "cost_usd": result.cost_usd,
                        "total_tokens": result.prompt_tokens + result.completion_tokens,
                        "latency_ms": result.latency_ms,
                    }
                ),
            }
        except Exception as exc:  # noqa: BLE001
            log.error("chat.stream_failed", error=str(exc))
            yield {"event": "error", "data": json.dumps({"message": "Turn failed"})}
        finally:
            yield {"event": "done", "data": "{}"}

    return EventSourceResponse(generator())


@router.get("/sessions", response_model=list[SessionOut], summary="List conversations")
async def list_sessions(
    principal: PrincipalDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[SessionOut]:
    async with session_scope() as session:
        rows = await ChatRepository(session).list_sessions(
            principal.tenant_id, principal.subject, limit=limit, offset=offset
        )
    return [
        SessionOut(
            session_id=r.id,
            title=r.title,
            status=r.status,
            channel=r.channel,
            message_count=r.message_count,
            total_tokens=r.total_tokens,
            total_cost_usd=float(r.total_cost_usd),
            last_activity_at=r.last_activity_at,
            created_at=r.created_at,
        )
        for r in rows
    ]


@router.get(
    "/sessions/{session_id}", response_model=HistoryResponse, summary="Conversation history"
)
async def get_history(
    session_id: uuid.UUID,
    principal: PrincipalDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> HistoryResponse:
    async with session_scope() as session:
        repo = ChatRepository(session)
        chat_session = await repo.get_or_create_session(
            session_id=session_id, tenant_id=principal.tenant_id, user_id=principal.subject
        )
        if chat_session.id != session_id:
            raise NotFoundError("Conversation not found")
        rows = await repo.history(session_id, limit=limit)

    return HistoryResponse(
        session_id=session_id,
        messages=[
            MessageOut(
                role=r.role,
                content=r.content,
                agent=r.agent,
                intent=r.intent,
                citations=r.citations,
                created_at=r.created_at.isoformat(),
            )
            for r in rows
        ],
    )


@router.delete(
    "/sessions/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Clear session state",
)
async def clear_session(session_id: uuid.UUID, principal: PrincipalDep) -> None:
    """Drops the Redis hot state. The Postgres archive is retained for audit."""
    orchestrator = get_orchestrator()
    await orchestrator.sessions.clear_history(str(session_id))
    await orchestrator.sessions.drop_shared(str(session_id))
    log.info("chat.session_cleared", session_id=str(session_id), user=principal.subject)


@router.get(
    "/approvals",
    response_model=list[ApprovalOut],
    summary="Pending approvals",
    dependencies=[Depends(require_role("agent_operator"))],
)
async def pending_approvals(principal: PrincipalDep) -> list[ApprovalOut]:
    async with session_scope() as session:
        rows = await ApprovalRepository(session).pending(principal.tenant_id)
    return [
        ApprovalOut(
            approval_id=r.id,
            session_id=r.session_id,
            intent=r.intent,
            action=r.action,
            payload=r.payload,
            status=r.status,
            risk_score=r.risk_score,
            requested_by=r.requested_by,
            created_at=r.created_at,
            expires_at=r.expires_at,
        )
        for r in rows
    ]


@router.post(
    "/approvals/{approval_id}",
    response_model=ChatResponse,
    summary="Decide an approval and resume the conversation",
    dependencies=[Depends(require_role("agent_operator"))],
)
async def decide_approval(
    approval_id: uuid.UUID, payload: ApprovalDecisionRequest, principal: PrincipalDep
) -> ChatResponse:
    """Record the decision, then resume the interrupted graph thread.

    The graph picks up inside the approval node with the staged action intact --
    no state is rebuilt and no tool call is repeated.
    """
    async with session_scope() as session:
        request = await ApprovalRepository(session).decide(
            approval_id,
            decision=payload.decision,
            decided_by=principal.subject,
            note=payload.note,
        )
        session_id = request.session_id

    result = await get_orchestrator().resume_after_approval(
        session_id=session_id,
        decision=payload.decision,
        decided_by=principal.subject,
        note=payload.note,
        principal=principal,
    )
    log.info(
        "chat.approval_decided",
        approval_id=str(approval_id),
        decision=payload.decision,
        by=principal.subject,
    )
    return ChatResponse(
        session_id=result.session_id,
        turn_id=result.turn_id,
        answer=result.answer,
        status=result.status,
        intent=result.intent,
        agent=result.agent,
        usage=UsageOut(latency_ms=result.latency_ms),
        trace_id=result.trace_id,
    )
