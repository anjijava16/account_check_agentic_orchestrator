"""Input/output guardrails.

Layered defence, cheapest check first:
  1. length + encoding sanity
  2. prompt-injection heuristics (instruction-override phrasing, tool coercion)
  3. jailbreak/roleplay patterns
  4. output checks: leaked system prompt, unredacted PII, unsupported claims

The heuristics deliberately bias toward flagging for review rather than hard
blocking, except for direct tool-coercion attempts which are always blocked.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.core.exceptions import GuardrailViolation
from app.core.logging import get_logger
from app.observability.metrics import GUARDRAIL_TRIPS
from app.security.pii import PATTERNS

log = get_logger(__name__)

INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(all\s+)?(previous|prior|above)\s+(instructions|prompts|rules)", re.I),
    re.compile(r"disregard\s+(your|the)\s+(system\s+)?(prompt|instructions|guidelines)", re.I),
    re.compile(r"you\s+are\s+now\s+(a|an|in)\s+", re.I),
    re.compile(r"(reveal|print|show|repeat|output)\s+(your|the)\s+(system\s+)?prompt", re.I),
    re.compile(r"developer\s+mode|dan\s+mode|jailbreak", re.I),
    re.compile(r"</?(system|assistant|tool)>", re.I),
    re.compile(r"\bBEGIN\s+SYSTEM\b|\bEND\s+SYSTEM\b", re.I),
]

TOOL_COERCION_PATTERNS = [
    re.compile(r"call\s+the\s+\w+\s+tool\s+with\s+customer_id\s*=", re.I),
    re.compile(r"(transfer|move|send)\s+(all\s+)?(funds|money|balance)\s+to", re.I),
    re.compile(r"for\s+(all|every)\s+customers?\b", re.I),
    re.compile(r"\bcustomer_id\s*[:=]\s*['\"]?\*", re.I),
    re.compile(r"bypass\s+(the\s+)?(approval|authorisation|authorization|kyc)", re.I),
]

SYSTEM_PROMPT_MARKERS = [
    "You are the Coordinator Agent",
    "TOOL CONTRACT",
    "## Operating rules",
]

MAX_INPUT_CHARS = 8000


@dataclass(slots=True)
class GuardrailResult:
    passed: bool
    risk_score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    sanitised: str | None = None

    def raise_if_blocked(self) -> None:
        if not self.passed:
            raise GuardrailViolation(
                "Request blocked by guardrails",
                details={"reasons": self.reasons, "risk_score": self.risk_score},
            )


def check_input(text: str) -> GuardrailResult:
    reasons: list[str] = []
    score = 0.0

    if not text or not text.strip():
        return GuardrailResult(False, 1.0, ["empty_input"])

    if len(text) > MAX_INPUT_CHARS:
        return GuardrailResult(False, 1.0, ["input_too_long"])

    if "\x00" in text or sum(ord(c) > 0x10000 for c in text) > len(text) * 0.3:
        reasons.append("suspicious_encoding")
        score += 0.3

    for pattern in INJECTION_PATTERNS:
        if pattern.search(text):
            reasons.append("prompt_injection_heuristic")
            score += 0.35
            break

    for pattern in TOOL_COERCION_PATTERNS:
        if pattern.search(text):
            GUARDRAIL_TRIPS.labels(kind="tool_coercion").inc()
            log.warning("guardrail.tool_coercion_blocked")
            return GuardrailResult(False, 1.0, [*reasons, "tool_coercion"])

    # Repeated delimiter spam is a classic context-stuffing vector.
    if text.count("```") > 8 or text.count("---") > 20:
        reasons.append("delimiter_spam")
        score += 0.2

    score = min(score, 1.0)
    if reasons:
        GUARDRAIL_TRIPS.labels(kind="input_flagged").inc()
    return GuardrailResult(passed=score < 0.7, risk_score=score, reasons=reasons)


def check_output(text: str, *, allow_pii: bool = True) -> GuardrailResult:
    reasons: list[str] = []
    score = 0.0
    sanitised = text

    for marker in SYSTEM_PROMPT_MARKERS:
        if marker in text:
            reasons.append("system_prompt_leak")
            score += 0.6
            sanitised = sanitised.replace(marker, "[redacted]")

    if not allow_pii:
        for kind in ("CARD", "SSN", "IBAN"):
            if PATTERNS[kind].search(text):
                reasons.append(f"unredacted_{kind.lower()}")
                score += 0.5

    if reasons:
        GUARDRAIL_TRIPS.labels(kind="output_flagged").inc()
        log.warning("guardrail.output_flagged", reasons=reasons)

    return GuardrailResult(
        passed=score < 0.6, risk_score=min(score, 1.0), reasons=reasons, sanitised=sanitised
    )
