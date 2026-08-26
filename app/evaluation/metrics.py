"""Evaluation metrics.

Deterministic metrics first (they're free, fast, and never flake), LLM-judged
metrics only where a deterministic proxy genuinely can't work.

  routing_accuracy    -- deterministic: predicted intent vs gold
  tool_correctness    -- deterministic: expected tools ⊆ called tools
  citation_coverage   -- deterministic: are claims backed by a citation
  context_precision   -- deterministic: retrieved chunks that overlap the gold answer
  faithfulness        -- LLM judge: is every claim supported by the context
  answer_relevance    -- LLM judge: does it answer the question asked
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from app.core.logging import get_logger
from app.llm.gateway import get_gateway

log = get_logger(__name__)

TOKEN = re.compile(r"[a-z0-9]+")
STOP = {"the", "a", "an", "is", "are", "of", "to", "in", "for", "on", "and", "or", "your", "you"}


@dataclass(slots=True)
class MetricScore:
    metric: str
    score: float
    passed: bool
    detail: dict[str, Any]


def _tokens(text: str) -> set[str]:
    return {t for t in TOKEN.findall((text or "").lower()) if t not in STOP and len(t) > 2}


# ------------------------------------------------------------- deterministic
def routing_accuracy(predicted: str | None, expected: str | None) -> MetricScore:
    hit = bool(predicted) and predicted == expected
    return MetricScore(
        "routing_accuracy", 1.0 if hit else 0.0, hit,
        {"predicted": predicted, "expected": expected},
    )


def tool_correctness(called: list[str], expected: list[str]) -> MetricScore:
    if not expected:
        return MetricScore("tool_correctness", 1.0, True, {"note": "no tools expected"})
    called_set = {c.split("__", 1)[-1] for c in called}
    expected_set = {e.split("__", 1)[-1] for e in expected}
    matched = expected_set & called_set
    score = len(matched) / len(expected_set)
    return MetricScore(
        "tool_correctness", round(score, 4), score >= 0.99,
        {"expected": sorted(expected_set), "called": sorted(called_set),
         "missing": sorted(expected_set - called_set)},
    )


def citation_coverage(answer: str, citations: list[dict]) -> MetricScore:
    """Any answer containing a factual claim should carry at least one citation
    when it came from the knowledge base."""
    has_claim = bool(re.search(r"\b(\d|policy|terms|days|fee|rate|must|require)\b", answer, re.I))
    if not has_claim:
        return MetricScore("citation_coverage", 1.0, True, {"note": "no citable claim"})
    score = 1.0 if citations else 0.0
    return MetricScore(
        "citation_coverage", score, bool(citations), {"citation_count": len(citations)}
    )


def context_precision(retrieved: list[str], reference: str) -> MetricScore:
    """Fraction of retrieved chunks that share meaningful vocabulary with the
    gold answer. Crude but stable, and it catches retrieval regressions fast."""
    if not retrieved:
        return MetricScore("context_precision", 0.0, False, {"note": "nothing retrieved"})
    ref = _tokens(reference)
    if not ref:
        return MetricScore("context_precision", 1.0, True, {"note": "empty reference"})
    useful = sum(1 for chunk in retrieved if len(_tokens(chunk) & ref) >= 3)
    score = useful / len(retrieved)
    return MetricScore(
        "context_precision", round(score, 4), score >= 0.5,
        {"useful": useful, "retrieved": len(retrieved)},
    )


def answer_similarity(prediction: str, reference: str) -> MetricScore:
    """Token-level F1 against the gold answer. Not a quality judgement -- a
    regression alarm."""
    pred, ref = _tokens(prediction), _tokens(reference)
    if not pred or not ref:
        return MetricScore("answer_similarity", 0.0, False, {})
    overlap = len(pred & ref)
    precision = overlap / len(pred)
    recall = overlap / len(ref)
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return MetricScore(
        "answer_similarity", round(f1, 4), f1 >= 0.4,
        {"precision": round(precision, 4), "recall": round(recall, 4)},
    )


# ----------------------------------------------------------------- LLM judge
JUDGE_FAITHFULNESS = """You are grading a banking assistant for faithfulness.

CONTEXT:
{context}

ANSWER:
{answer}

Score 0.0-1.0: what fraction of the answer's factual claims are directly
supported by the context? A claim not in the context scores against it, even if
it is true in general. Numbers, dates, fees and references must match exactly.

Reply with JSON only: {{"score": 0.0, "unsupported_claims": ["..."]}}"""

JUDGE_RELEVANCE = """Grade whether this answer addresses the question asked.

QUESTION: {question}
ANSWER: {answer}

Score 0.0-1.0. A correct but off-topic answer scores low. A partial answer that
addresses the actual question scores moderately.

Reply with JSON only: {{"score": 0.0, "reason": "one clause"}}"""


async def _judge(prompt: str, metric: str, threshold: float) -> MetricScore:
    from app.core.config import settings

    try:
        response = await get_gateway().complete(
            [{"role": "user", "content": prompt}],
            model=settings.router_model,
            temperature=0.0,
            max_tokens=300,
            response_format={"type": "json_object"},
            metadata={"purpose": "evaluation"},
        )
        payload = json.loads(re.sub(r"^```(?:json)?|```$", "", response.content.strip(), flags=re.S))
        score = float(payload.get("score", 0.0))
        return MetricScore(metric, round(score, 4), score >= threshold, payload)
    except Exception as exc:  # noqa: BLE001
        log.warning("eval.judge_failed", metric=metric, error=str(exc))
        return MetricScore(metric, 0.0, False, {"error": str(exc)[:200]})


async def faithfulness(answer: str, contexts: list[str]) -> MetricScore:
    if not contexts:
        return MetricScore("faithfulness", 1.0, True, {"note": "no context to contradict"})
    prompt = JUDGE_FAITHFULNESS.format(
        context="\n---\n".join(contexts[:6])[:6000], answer=answer[:2000]
    )
    return await _judge(prompt, "faithfulness", 0.8)


async def answer_relevance(question: str, answer: str) -> MetricScore:
    prompt = JUDGE_RELEVANCE.format(question=question[:1000], answer=answer[:2000])
    return await _judge(prompt, "answer_relevance", 0.7)
