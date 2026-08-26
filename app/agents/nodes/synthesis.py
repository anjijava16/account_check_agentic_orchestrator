"""Final composition + output guardrails.

Two jobs:
  1. Attach citations to knowledge-lookup answers so a claim can be traced back
     to a document and page.
  2. Run output guardrails before anything reaches the customer.

Small-talk and out-of-scope turns are answered here directly -- routing them to
a specialist with a full toolset would be pure waste.
"""
from __future__ import annotations

from langchain_core.messages import AIMessage

from app.agents.state import AgentState
from app.core.constants import Intent
from app.core.logging import get_logger
from app.observability.metrics import GRAPH_TURNS
from app.security.guardrails import check_output

log = get_logger(__name__)

SMALL_TALK = "Hello. I can help with balances, transactions, statements, and servicing requests. What do you need?"
OUT_OF_SCOPE = (
    "That's outside what I can help with here. I can answer questions about your "
    "accounts, transactions, statements, and servicing requests."
)


async def synthesis_node(state: AgentState) -> dict:
    intent = state.get("intent")
    answer = state.get("final_answer", "")

    if not answer:
        if intent == Intent.SMALL_TALK:
            answer = SMALL_TALK
        elif intent == Intent.OUT_OF_SCOPE:
            answer = OUT_OF_SCOPE
        else:
            answer = (
                "I couldn't complete that just now. Please try again, or I can pass you "
                "to a colleague."
            )

    citations = state.get("citations", [])
    if citations and intent == Intent.KNOWLEDGE_LOOKUP:
        answer = _append_sources(answer, citations)

    result = check_output(answer, allow_pii=True)
    if not result.passed:
        log.warning("synthesis.output_blocked", reasons=result.reasons)
        answer = (
            "I'm not able to share that. If you need account details, I can help "
            "with the parts I'm permitted to show."
        )

    GRAPH_TURNS.labels(outcome="error" if state.get("error") else "ok").inc()
    return {
        "final_answer": result.sanitised or answer,
        "messages": [AIMessage(content=answer)],
        "guardrail_flags": result.reasons,
        "step_count": state.get("step_count", 0) + 1,
    }


def _append_sources(answer: str, citations: list[dict]) -> str:
    seen: list[str] = []
    for citation in citations:
        label = citation.get("filename") or citation.get("document_id", "")
        page = citation.get("page")
        section = citation.get("section")
        parts = [label]
        if section:
            parts.append(str(section))
        if page:
            parts.append(f"p.{page}")
        rendered = " — ".join(p for p in parts if p)
        if rendered and rendered not in seen:
            seen.append(rendered)
    if not seen:
        return answer
    return answer + "\n\nSources:\n" + "\n".join(f"· {s}" for s in seen[:4])
