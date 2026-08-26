"""Entry node: sanity-check and sanitise the incoming turn."""
from __future__ import annotations

from langchain_core.messages import AIMessage

from app.agents.state import AgentState
from app.core.logging import get_logger
from app.security.guardrails import check_input

log = get_logger(__name__)

REFUSAL = (
    "I can't help with that request. If you have a question about your accounts, "
    "transactions, or a servicing request, I'm happy to help with that."
)


async def guardrail_node(state: AgentState) -> dict:
    result = check_input(state.get("user_input", ""))

    if not result.passed:
        log.warning(
            "guardrail.turn_blocked",
            reasons=result.reasons,
            session_id=state.get("session_id"),
        )
        return {
            "guardrail_flags": result.reasons,
            "risk_score": result.risk_score,
            "final_answer": REFUSAL,
            "error": "guardrail_blocked",
            "messages": [AIMessage(content=REFUSAL)],
        }

    return {
        "sanitised_input": result.sanitised or state["user_input"],
        "guardrail_flags": result.reasons,
        "risk_score": result.risk_score,
    }
