"""Orchestrator.

The seam between HTTP and the graph. Everything that isn't agent reasoning
lives here so the nodes stay testable:

  * session resolution (Redis hot path + Postgres durable record)
  * per-session turn locking so two concurrent requests can't interleave writes
  * graph invocation with the right thread_id (thread_id == session_id, which
    is what makes the checkpointer resume the right conversation)
  * interrupt detection -> pending_approval response
  * resume after approval
  * persistence of the turn, cost attribution, sampled evaluation
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Command

from app.agents.graph import get_graph
from app.agents.state import new_state
from app.core.config import settings
from app.core.constants import MessageRole
from app.core.exceptions import PlatformError
from app.core.logging import get_logger
from app.db.repositories.chat import ChatRepository
from app.db.session import session_scope
from app.llm.cost_tracker import get_cost_tracker
from app.memory.session_store import SessionStore
from app.observability.callbacks import TelemetryCallbackHandler
from app.observability.tracing import current_trace_id, span
from app.security.auth import Principal

log = get_logger(__name__)


@dataclass(slots=True)
class TurnResult:
    session_id: uuid.UUID
    turn_id: uuid.UUID
    answer: str
    intent: str | None = None
    agent: str | None = None
    status: str = "completed"  # completed | pending_approval | error
    citations: list[dict[str, Any]] = field(default_factory=list)
    tool_traces: list[dict[str, Any]] = field(default_factory=list)
    approval: dict[str, Any] | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    model: str | None = None
    latency_ms: int = 0
    trace_id: str | None = None
    guardrail_flags: list[str] = field(default_factory=list)


class Orchestrator:
    def __init__(self) -> None:
        self.sessions = SessionStore()
        self.cost = get_cost_tracker()

    # ------------------------------------------------------------- public
    async def handle_turn(
        self,
        *,
        message: str,
        principal: Principal,
        session_id: uuid.UUID | None = None,
        request_id: str | None = None,
    ) -> TurnResult:
        started = time.perf_counter()
        turn_id = uuid.uuid4()

        async with session_scope() as session:
            chat = await ChatRepository(session).get_or_create_session(
                session_id=session_id,
                tenant_id=principal.tenant_id,
                user_id=principal.subject,
                channel=principal.channel,
            )
            sid = chat.id

        lock_token = str(turn_id)
        if not await self.sessions.acquire_turn_lock(str(sid), lock_token):
            raise PlatformError(
                "Another message on this conversation is still being processed",
                details={"session_id": str(sid)},
            )

        try:
            return await self._run(
                message=message,
                principal=principal,
                session_id=sid,
                turn_id=turn_id,
                request_id=request_id,
                started=started,
            )
        finally:
            await self.sessions.release_turn_lock(str(sid), lock_token)

    async def resume_after_approval(
        self,
        *,
        session_id: uuid.UUID,
        decision: str,
        decided_by: str,
        note: str | None = None,
        principal: Principal | None = None,
    ) -> TurnResult:
        """Resume an interrupted thread once a human has decided."""
        started = time.perf_counter()
        turn_id = uuid.uuid4()
        config = self._config(session_id, turn_id, principal)

        with span("orchestrator.resume", **{"session.id": str(session_id)}):
            final = await get_graph().ainvoke(
                Command(resume={"decision": decision, "decided_by": decided_by, "note": note}),
                config=config,
            )

        result = self._to_result(final, session_id, turn_id, started)
        await self._persist_turn(result, principal, user_message=None)
        return result

    async def history(self, session_id: uuid.UUID, limit: int = 20) -> list[dict[str, Any]]:
        cached = await self.sessions.history(str(session_id), limit=limit)
        if cached:
            return cached
        async with session_scope() as session:
            rows = await ChatRepository(session).history(session_id, limit=limit)
        return [
            {
                "role": r.role,
                "content": r.content,
                "agent": r.agent,
                "intent": r.intent,
                "citations": r.citations,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ]

    # ------------------------------------------------------------ internal
    async def _run(
        self,
        *,
        message: str,
        principal: Principal,
        session_id: uuid.UUID,
        turn_id: uuid.UUID,
        request_id: str | None,
        started: float,
    ) -> TurnResult:
        history = await self.sessions.history(str(session_id), limit=8)
        messages = [
            HumanMessage(content=h["content"])
            if h["role"] == MessageRole.USER
            else AIMessage(content=h["content"])
            for h in history
        ]

        state = new_state(
            session_id=str(session_id),
            turn_id=str(turn_id),
            request_id=request_id or str(uuid.uuid4()),
            tenant_id=principal.tenant_id,
            user_id=principal.subject,
            customer_id=(principal.customer_ids or [None])[0],
            roles=principal.roles or ["customer"],
            channel=principal.channel,
            user_input=message,
            messages=messages + [HumanMessage(content=message)],
        )

        config = self._config(session_id, turn_id, principal)

        with span(
            "orchestrator.turn",
            **{
                "session.id": str(session_id),
                "turn.id": str(turn_id),
                "user.tenant": principal.tenant_id,
            },
        ):
            final = await asyncio.wait_for(
                get_graph().ainvoke(state, config=config),
                timeout=settings.request_timeout_seconds,
            )

        result = self._to_result(final, session_id, turn_id, started)
        await self._persist_turn(result, principal, user_message=message)
        await self._maybe_evaluate(result, message)
        return result

    def _config(
        self, session_id: uuid.UUID, turn_id: uuid.UUID, principal: Principal | None
    ) -> dict[str, Any]:
        return {
            "configurable": {"thread_id": str(session_id), "checkpoint_ns": "chat"},
            "recursion_limit": settings.agent_recursion_limit,
            "callbacks": [
                TelemetryCallbackHandler(session_id=session_id, turn_id=turn_id)
            ],
            "metadata": {
                "tenant_id": principal.tenant_id if principal else "default",
                "turn_id": str(turn_id),
            },
        }

    def _to_result(
        self, final: dict[str, Any], session_id: uuid.UUID, turn_id: uuid.UUID, started: float
    ) -> TurnResult:
        interrupts = final.get("__interrupt__") or []
        latency_ms = int((time.perf_counter() - started) * 1000)

        if interrupts:
            payload = getattr(interrupts[0], "value", interrupts[0])
            return TurnResult(
                session_id=session_id,
                turn_id=turn_id,
                answer=(
                    "I've submitted that for approval. A colleague will verify it — "
                    f"your reference is {payload.get('reference', 'pending')}."
                ),
                status="pending_approval",
                approval=payload,
                intent=final.get("intent"),
                agent=final.get("target_agent"),
                latency_ms=latency_ms,
                trace_id=current_trace_id(),
            )

        return TurnResult(
            session_id=session_id,
            turn_id=turn_id,
            answer=final.get("final_answer", ""),
            intent=final.get("intent"),
            agent=final.get("target_agent"),
            status="error" if final.get("error") else "completed",
            citations=final.get("citations", []),
            tool_traces=final.get("tool_traces", []),
            prompt_tokens=final.get("prompt_tokens", 0),
            completion_tokens=final.get("completion_tokens", 0),
            cost_usd=final.get("cost_usd", 0.0),
            model=final.get("model_used"),
            latency_ms=latency_ms,
            trace_id=current_trace_id(),
            guardrail_flags=final.get("guardrail_flags", []),
        )

    async def _persist_turn(
        self, result: TurnResult, principal: Principal | None, user_message: str | None
    ) -> None:
        try:
            async with session_scope() as session:
                repo = ChatRepository(session)
                if user_message is not None:
                    await repo.add_message(
                        session_id=result.session_id,
                        role=MessageRole.USER,
                        content=user_message,
                        trace_id=result.trace_id,
                    )
                await repo.add_message(
                    session_id=result.session_id,
                    role=MessageRole.ASSISTANT,
                    content=result.answer,
                    agent=result.agent,
                    intent=result.intent,
                    model=result.model,
                    prompt_tokens=result.prompt_tokens,
                    completion_tokens=result.completion_tokens,
                    latency_ms=result.latency_ms,
                    citations=result.citations,
                    trace_id=result.trace_id,
                    message_metadata={
                        "status": result.status,
                        "tools": [t.get("tool") for t in result.tool_traces],
                        "guardrail_flags": result.guardrail_flags,
                    },
                )
        except Exception as exc:  # noqa: BLE001
            log.error("orchestrator.persist_failed", error=str(exc))

        if user_message is not None:
            await self.sessions.append_turn(
                str(result.session_id), MessageRole.USER, user_message
            )
        await self.sessions.append_turn(
            str(result.session_id),
            MessageRole.ASSISTANT,
            result.answer,
            agent=result.agent,
            intent=result.intent,
        )
        await self.sessions.set_shared_many(
            str(result.session_id),
            {"last_intent": result.intent, "last_agent": result.agent},
        )

    async def _maybe_evaluate(self, result: TurnResult, question: str) -> None:
        import random

        if not settings.eval_enabled or random.random() > settings.eval_sample_rate:  # noqa: S311
            return
        try:
            from arq import create_pool

            from app.workers.queue import JOB_RUN_ONLINE_EVAL, redis_settings

            pool = await create_pool(redis_settings())
            await pool.enqueue_job(
                JOB_RUN_ONLINE_EVAL,
                {
                    "session_id": str(result.session_id),
                    "question": question,
                    "answer": result.answer,
                    "citations": result.citations,
                    "intent": result.intent,
                },
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("orchestrator.eval_enqueue_failed", error=str(exc))


_orchestrator: Orchestrator | None = None


def get_orchestrator() -> Orchestrator:
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = Orchestrator()
    return _orchestrator
