"""Authorisation.

Policy-as-data: a declarative map of (role -> allowed intents, tools, data
classifications). Every agent tool call passes through `enforce_tool` before it
reaches the MCP layer, so a jailbroken prompt still cannot invoke a tool the
caller's role has no right to.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.constants import HIGH_RISK_INTENTS, Intent
from app.core.exceptions import AuthorizationError
from app.core.logging import get_logger
from app.security.auth import Principal

log = get_logger(__name__)

DEFAULT_POLICY: dict[str, dict[str, Any]] = {
    "customer": {
        "intents": [
            Intent.BALANCE_ENQUIRY,
            Intent.TRANSACTION_DETAILS,
            Intent.STATEMENT_REQUEST,
            Intent.CHANGE_OF_ADDRESS,
            Intent.CHEQUE_BOOK_REQUEST,
            Intent.KYC_UPDATE,
            Intent.KNOWLEDGE_LOOKUP,
            Intent.SMALL_TALK,
        ],
        "tools": [
            "accounts__get_balance",
            "accounts__list_accounts",
            "accounts__get_account_details",
            "transactions__search_transactions",
            "transactions__get_transaction",
            "transactions__request_statement",
            "service__update_address",
            "service__request_cheque_book",
            "service__start_kyc_update",
            "service__search_knowledge_base",
        ],
        "max_classification": "confidential",
        "own_data_only": True,
    },
    "agent_operator": {
        "intents": list(Intent),
        "tools": ["*"],
        "max_classification": "restricted",
        "own_data_only": False,
    },
    "auditor": {
        "intents": [Intent.KNOWLEDGE_LOOKUP],
        "tools": ["service__search_knowledge_base"],
        "max_classification": "restricted",
        "own_data_only": False,
    },
}

CLASSIFICATION_ORDER = {"public": 0, "internal": 1, "confidential": 2, "restricted": 3}


@dataclass(slots=True)
class AuthzDecision:
    allowed: bool
    reason: str = ""
    requires_approval: bool = False


class PolicyEngine:
    def __init__(self, policy: dict[str, dict[str, Any]] | None = None) -> None:
        self.policy = policy or DEFAULT_POLICY

    @classmethod
    def from_file(cls, path: str) -> PolicyEngine:
        file = Path(path)
        if not file.exists():
            log.info("authz.policy_file_missing", path=path, note="using built-in default")
            return cls()
        try:
            import yaml  # type: ignore

            loaded = yaml.safe_load(file.read_text()) or {}
            return cls(loaded.get("roles", DEFAULT_POLICY))
        except Exception as exc:  # noqa: BLE001
            log.error("authz.policy_load_failed", error=str(exc))
            return cls()

    def _roles(self, principal: Principal) -> list[str]:
        return principal.roles or ["customer"]

    def check_intent(self, principal: Principal, intent: str) -> AuthzDecision:
        for role in self._roles(principal):
            allowed = {str(i) for i in self.policy.get(role, {}).get("intents", [])}
            if intent in allowed:
                return AuthzDecision(
                    allowed=True, requires_approval=intent in {str(i) for i in HIGH_RISK_INTENTS}
                )
        return AuthzDecision(False, f"Role(s) {self._roles(principal)} cannot perform {intent}")

    def check_tool(self, principal: Principal, tool_name: str) -> AuthzDecision:
        for role in self._roles(principal):
            allowed = self.policy.get(role, {}).get("tools", [])
            if "*" in allowed or tool_name in allowed:
                return AuthzDecision(True)
        return AuthzDecision(False, f"Tool {tool_name} not permitted for {self._roles(principal)}")

    def check_classification(self, principal: Principal, classification: str) -> AuthzDecision:
        want = CLASSIFICATION_ORDER.get(classification, 3)
        for role in self._roles(principal):
            ceiling = CLASSIFICATION_ORDER.get(
                self.policy.get(role, {}).get("max_classification", "internal"), 1
            )
            if want <= ceiling:
                return AuthzDecision(True)
        return AuthzDecision(False, f"Data classification '{classification}' above role ceiling")

    def check_ownership(self, principal: Principal, customer_id: str | None) -> AuthzDecision:
        if customer_id is None:
            return AuthzDecision(True)
        for role in self._roles(principal):
            if not self.policy.get(role, {}).get("own_data_only", True):
                return AuthzDecision(True)
        if principal.owns(customer_id):
            return AuthzDecision(True)
        return AuthzDecision(False, "Caller may only access their own customer records")


_engine: PolicyEngine | None = None


def get_policy_engine() -> PolicyEngine:
    global _engine
    if _engine is None:
        from app.core.config import settings

        _engine = PolicyEngine.from_file(settings.authz_policy_file)
    return _engine


def enforce_tool(principal: Principal, tool_name: str, arguments: dict[str, Any]) -> None:
    engine = get_policy_engine()
    decision = engine.check_tool(principal, tool_name)
    if not decision.allowed:
        log.warning("authz.tool_denied", tool=tool_name, subject=principal.subject)
        raise AuthorizationError(decision.reason, details={"tool": tool_name})

    customer_id = arguments.get("customer_id")
    ownership = engine.check_ownership(principal, customer_id)
    if not ownership.allowed:
        log.warning(
            "authz.ownership_denied", tool=tool_name, subject=principal.subject,
            customer_id=customer_id,
        )
        raise AuthorizationError(ownership.reason, details={"tool": tool_name})


def enforce_intent(principal: Principal, intent: str) -> AuthzDecision:
    decision = get_policy_engine().check_intent(principal, intent)
    if not decision.allowed:
        raise AuthorizationError(decision.reason, details={"intent": intent})
    return decision
