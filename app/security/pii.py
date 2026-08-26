"""PII Redaction.

Sits between the agent core and anything that leaves the trust boundary:
third-party LLMs, logs, traces, and the eval store. Two backends:

  * regex  -- deterministic, zero-dependency, fast enough for the hot path
  * presidio -- NER-backed, catches names/addresses regex can't

Redaction is *reversible within a request* via the returned vault, so tool
calls can still be executed with real values after the model has planned with
placeholders.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

PATTERNS: dict[str, re.Pattern[str]] = {
    "ACCOUNT": re.compile(r"\b\d{8,17}\b"),
    "CARD": re.compile(r"\b(?:\d[ -]*?){13,19}\b"),
    "SSN": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "IBAN": re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b"),
    "SORT_CODE": re.compile(r"\b\d{2}-\d{2}-\d{2}\b"),
    "EMAIL": re.compile(r"\b[\w.%+-]+@[\w.-]+\.[A-Za-z]{2,}\b"),
    "PHONE": re.compile(r"(?:\+\d{1,3}[ -]?)?(?:\(?\d{3}\)?[ -]?)\d{3}[ -]?\d{4}\b"),
    "DOB": re.compile(r"\b(?:19|20)\d{2}[-/](?:0[1-9]|1[0-2])[-/](?:0[1-9]|[12]\d|3[01])\b"),
    "IP": re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    "POSTCODE": re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2}\b"),
}

# Order matters: longer/more-specific patterns win before generic digit runs.
PRIORITY = ["CARD", "IBAN", "SSN", "SORT_CODE", "DOB", "EMAIL", "PHONE", "POSTCODE", "IP", "ACCOUNT"]


def _luhn(number: str) -> bool:
    digits = [int(d) for d in re.sub(r"\D", "", number)]
    if len(digits) < 13:
        return False
    checksum, parity = 0, len(digits) % 2
    for i, d in enumerate(digits):
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        checksum += d
    return checksum % 10 == 0


@dataclass(slots=True)
class RedactionResult:
    text: str
    vault: dict[str, str] = field(default_factory=dict)  # placeholder -> original
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def found_pii(self) -> bool:
        return bool(self.vault)

    def restore(self, text: str) -> str:
        for placeholder, original in self.vault.items():
            text = text.replace(placeholder, original)
        return text


class RegexRedactor:
    def redact(self, text: str) -> RedactionResult:
        if not text:
            return RedactionResult(text="")
        vault: dict[str, str] = {}
        counts: dict[str, int] = {}
        out = text

        for kind in PRIORITY:
            pattern = PATTERNS[kind]

            def _sub(match: re.Match[str], _kind: str = kind) -> str:
                value = match.group(0)
                if _kind == "CARD" and not _luhn(value):
                    return value
                if _kind == "ACCOUNT" and len(re.sub(r"\D", "", value)) < 8:
                    return value
                token = f"<{_kind}_{uuid.uuid4().hex[:8]}>"
                vault[token] = value
                counts[_kind] = counts.get(_kind, 0) + 1
                return token

            out = pattern.sub(_sub, out)
        return RedactionResult(text=out, vault=vault, counts=counts)


class PresidioRedactor:
    """Optional NER-backed redactor; falls back to regex when unavailable."""

    def __init__(self) -> None:
        self._analyzer = None
        try:
            from presidio_analyzer import AnalyzerEngine  # type: ignore

            self._analyzer = AnalyzerEngine()
        except Exception as exc:  # noqa: BLE001
            log.warning("pii.presidio_unavailable", error=str(exc))

    def redact(self, text: str) -> RedactionResult:
        if self._analyzer is None:
            return RegexRedactor().redact(text)
        results = self._analyzer.analyze(text=text, language="en")
        vault: dict[str, str] = {}
        counts: dict[str, int] = {}
        out = text
        for res in sorted(results, key=lambda r: r.start, reverse=True):
            original = text[res.start : res.end]
            token = f"<{res.entity_type}_{uuid.uuid4().hex[:8]}>"
            vault[token] = original
            counts[res.entity_type] = counts.get(res.entity_type, 0) + 1
            out = out[: res.start] + token + out[res.end :]
        merged = RegexRedactor().redact(out)
        vault.update(merged.vault)
        for k, v in merged.counts.items():
            counts[k] = counts.get(k, 0) + v
        return RedactionResult(text=merged.text, vault=vault, counts=counts)


_redactor: RegexRedactor | PresidioRedactor | None = None


def get_redactor() -> RegexRedactor | PresidioRedactor:
    global _redactor
    if _redactor is None:
        _redactor = (
            PresidioRedactor() if settings.pii_redaction_backend == "presidio" else RegexRedactor()
        )
    return _redactor


def redact(text: str) -> RedactionResult:
    if not settings.pii_redaction_enabled:
        return RedactionResult(text=text)
    return get_redactor().redact(text)


def redact_text(text: str) -> str:
    return redact(text).text


def redact_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Redact message content in place-safe fashion (returns copies)."""
    if not settings.pii_redaction_enabled:
        return messages
    redactor = get_redactor()
    out: list[dict[str, Any]] = []
    total = 0
    for msg in messages:
        copy = dict(msg)
        content = copy.get("content")
        if isinstance(content, str):
            result = redactor.redact(content)
            copy["content"] = result.text
            total += sum(result.counts.values())
        out.append(copy)
    if total:
        log.info("pii.redacted_outbound", entities=total, messages=len(messages))
    return out
