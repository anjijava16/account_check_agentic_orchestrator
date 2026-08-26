"""Coordinator Agent node.

Classifies intent and picks the specialist. Runs on the small, cheap model:
routing is a classification problem, not a reasoning problem, and paying
70B-model prices to decide "this is a balance question" is how agent platforms
end up with indefensible unit economics.

A deterministic keyword pre-pass short-circuits the obvious cases entirely --
roughly 40% of real traffic never needs a model call to route.
"""
from __future__ import annotations

import json
import re

from app.agents.prompts import COORDINATOR_PROMPT
from app.agents.state import AgentState
from app.core.constants import HIGH_RISK_INTENTS, INTENT_TO_AGENT, AgentName, Intent
from app.core.logging import get_logger
from app.llm.gateway import get_gateway
from app.observability.metrics import ROUTING_DECISIONS
from app.observability.tracing import span
from app.security.authz import get_policy_engine
from app.security.auth import Principal

log = get_logger(__name__)

FAST_PATH: list[tuple[re.Pattern[str], Intent]] = [
    (re.compile(r"\b(balance|how much (do i|have i)|available funds)\b", re.I), Intent.BALANCE_ENQUIRY),
    (re.compile(r"\b(statement|e-?statement)\b", re.I), Intent.STATEMENT_REQUEST),
    (re.compile(r"\b(cheque ?book|check ?book)\b", re.I), Intent.CHEQUE_BOOK_REQUEST),
    (re.compile(r"\b(kyc|know your customer|verify my identity|id verification)\b", re.I), Intent.KYC_UPDATE),
    (re.compile(r"\b(change|update).{0,20}\baddress\b|\bmoved (house|to)\b", re.I), Intent.CHANGE_OF_ADDRESS),
    (re.compile(r"\b(transaction|spent|spend|payment|charge|debited|credited)\b", re.I), Intent.TRANSACTION_DETAILS),
    (re.compile(r"^\s*(hi|hello|hey|good (morning|afternoon|evening)|thanks|thank you)\b", re.I), Intent.SMALL_TALK),
]


def _fast_route(text: str) -> Intent | None:
    for pattern, intent in FAST_PATH:
        if pattern.search(text):
            return intent
    return None


async def coordinator_node(state: AgentState) -> dict:
    text = state.get("sanitised_input") or state.get("user_input", "")

    with span("agent.coordinator", **{"agent.name": "coordinator"}):
        fast = _fast_route(text)
        if fast is not None:
            intent, confidence, rationale = fast, 0.85, "keyword fast-path"
            usage = None
        else:
            intent, confidence, rationale, usage = await _model_route(text, state)

        agent = INTENT_TO_AGENT.get(intent, AgentName.COORDINATOR)
        requires_approval = intent in HIGH_RISK_INTENTS

        # Authorisation is checked here, before any tool is bound to an agent.
        principal = Principal(
            subject=state.get("user_id", ""),
            tenant_id=state.get("tenant_id", "default"),
            roles=state.get("roles", ["customer"]),
            customer_ids=[state["customer_id"]] if state.get("customer_id") else [],
        )
        decision = get_policy_engine().check_intent(principal, str(intent))
        if not decision.allowed:
            log.warning("coordinator.intent_denied", intent=str(intent), reason=decision.reason)
            return {
                "intent": str(intent),
                "target_agent": AgentName.COORDINATOR,
                "final_answer": "I'm not able to help with that on this channel.",
                "error": "intent_not_permitted",
            }

    ROUTING_DECISIONS.labels(intent=str(intent), agent=str(agent)).inc()
    log.info(
        "coordinator.routed",
        intent=str(intent),
        agent=str(agent),
        confidence=confidence,
        fast_path=fast is not None,
    )

    update: dict = {
        "intent": str(intent),
        "intent_confidence": confidence,
        "target_agent": str(agent),
        "routing_rationale": rationale,
        "requires_approval": requires_approval,
        "step_count": state.get("step_count", 0) + 1,
    }
    if usage is not None:
        update["prompt_tokens"] = state.get("prompt_tokens", 0) + usage.prompt_tokens
        update["completion_tokens"] = state.get("completion_tokens", 0) + usage.completion_tokens
        update["cost_usd"] = round(state.get("cost_usd", 0.0) + usage.cost_usd, 6)
        update["model_used"] = usage.model
    return update


async def _model_route(text: str, state: AgentState):
    from app.core.config import settings

    history = [
        {"role": m.type if m.type in {"user", "assistant"} else "user", "content": m.content}
        for m in state.get("messages", [])[-4:]
    ]
    messages = [
        {"role": "system", "content": COORDINATOR_PROMPT},
        *history,
        {"role": "user", "content": text},
    ]

    try:
        response = await get_gateway().complete(
            messages,
            model=settings.router_model,
            temperature=0.0,
            max_tokens=200,
            response_format={"type": "json_object"},
            metadata={"agent": "coordinator", "session_id": state.get("session_id")},
        )
        payload = json.loads(_strip_fence(response.content))
        intent = Intent(payload.get("intent", Intent.OUT_OF_SCOPE))
        return (
            intent,
            float(payload.get("confidence", 0.5)),
            str(payload.get("rationale", ""))[:200],
            response.usage,
        )
    except (ValueError, KeyError, json.JSONDecodeError) as exc:
        log.warning("coordinator.parse_failed", error=str(exc))
        return Intent.KNOWLEDGE_LOOKUP, 0.3, "fallback after parse failure", None
    except Exception as exc:  # noqa: BLE001
        log.error("coordinator.route_failed", error=str(exc))
        return Intent.KNOWLEDGE_LOOKUP, 0.2, "fallback after model error", None


def _strip_fence(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.S)
    return cleaned or "{}"
