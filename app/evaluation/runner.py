"""Evaluation runner.

Two modes:
  offline -- replay a golden dataset through the real graph, score, persist.
             Run in CI; block a deploy when a metric regresses past its floor.
  online  -- sample a percentage of live turns and score them without a gold
             answer (faithfulness and citation coverage still work).
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import update

from app.core.logging import get_logger
from app.db.models import EvalResult, EvalRun
from app.db.session import session_scope
from app.evaluation.metrics import (
    MetricScore,
    answer_relevance,
    answer_similarity,
    citation_coverage,
    context_precision,
    faithfulness,
    routing_accuracy,
    tool_correctness,
)
from app.security.auth import Principal

log = get_logger(__name__)

# A metric below its floor fails the run. Tune these from a real baseline
# before turning them into a CI gate.
THRESHOLDS = {
    "routing_accuracy": 0.90,
    "faithfulness": 0.85,
    "answer_relevance": 0.80,
    "context_precision": 0.60,
    "citation_coverage": 0.90,
    "tool_correctness": 0.90,
    "answer_similarity": 0.35,
}


def load_dataset(path: str) -> list[dict[str, Any]]:
    file = Path(path)
    if not file.exists():
        log.warning("eval.dataset_missing", path=path)
        return []
    cases = []
    for line in file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            cases.append(json.loads(line))
    return cases


async def run_offline_suite(
    *,
    run_id: uuid.UUID,
    name: str,
    dataset_path: str,
    metrics: list[str],
    tenant_id: str = "default",
) -> dict[str, Any]:
    from app.agents.orchestrator import get_orchestrator

    cases = load_dataset(dataset_path)
    async with session_scope() as session:
        session.add(
            EvalRun(
                id=run_id, name=name, mode="offline", dataset=dataset_path,
                status="running", total_cases=len(cases),
            )
        )

    if not cases:
        async with session_scope() as session:
            await session.execute(
                update(EvalRun).where(EvalRun.id == run_id).values(status="no_dataset")
            )
        return {"run_id": str(run_id), "status": "no_dataset"}

    orchestrator = get_orchestrator()
    all_scores: dict[str, list[float]] = {}
    passed_cases = 0

    for case in cases:
        principal = Principal(
            subject=case.get("user_id", "eval-user"),
            tenant_id=tenant_id,
            roles=case.get("roles", ["customer"]),
            customer_ids=case.get("customer_ids", ["CUST-1001"]),
            scopes=["chat:write"],
        )
        try:
            result = await orchestrator.handle_turn(
                message=case["question"], principal=principal, session_id=None
            )
        except Exception as exc:  # noqa: BLE001
            log.error("eval.case_failed", case_id=case.get("id"), error=str(exc))
            continue

        contexts = [c.get("content", "") for c in case.get("contexts", [])]
        scores = await _score_case(case, result, contexts, metrics)

        case_passed = all(s.passed for s in scores)
        passed_cases += int(case_passed)

        async with session_scope() as session:
            for score in scores:
                all_scores.setdefault(score.metric, []).append(score.score)
                session.add(
                    EvalResult(
                        run_id=run_id,
                        case_id=str(case.get("id", uuid.uuid4())),
                        metric=score.metric,
                        score=score.score,
                        passed=score.passed,
                        question=case["question"],
                        prediction=result.answer,
                        reference=case.get("reference_answer"),
                        detail=score.detail,
                    )
                )

    aggregates = {
        metric: round(sum(values) / len(values), 4)
        for metric, values in all_scores.items()
        if values
    }
    regressions = [
        m for m, v in aggregates.items() if m in THRESHOLDS and v < THRESHOLDS[m]
    ]
    status = "failed" if regressions else "passed"

    async with session_scope() as session:
        await session.execute(
            update(EvalRun)
            .where(EvalRun.id == run_id)
            .values(
                status=status,
                passed_cases=passed_cases,
                aggregate_scores={**aggregates, "regressions": regressions},
            )
        )

    log.info(
        "eval.run_complete",
        run_id=str(run_id),
        status=status,
        cases=len(cases),
        passed=passed_cases,
        regressions=regressions,
    )
    return {
        "run_id": str(run_id),
        "status": status,
        "aggregates": aggregates,
        "regressions": regressions,
    }


async def _score_case(
    case: dict[str, Any], result: Any, contexts: list[str], metrics: list[str]
) -> list[MetricScore]:
    scores: list[MetricScore] = []
    retrieved = contexts or [c.get("section", "") for c in result.citations]
    called_tools = [t.get("tool", "") for t in result.tool_traces]

    if "routing_accuracy" in metrics:
        scores.append(routing_accuracy(result.intent, case.get("expected_intent")))
    if "tool_correctness" in metrics:
        scores.append(tool_correctness(called_tools, case.get("expected_tools", [])))
    if "citation_coverage" in metrics and case.get("expects_citations"):
        scores.append(citation_coverage(result.answer, result.citations))
    if "context_precision" in metrics and case.get("reference_answer"):
        scores.append(context_precision(retrieved, case["reference_answer"]))
    if "answer_similarity" in metrics and case.get("reference_answer"):
        scores.append(answer_similarity(result.answer, case["reference_answer"]))
    if "faithfulness" in metrics:
        scores.append(await faithfulness(result.answer, retrieved))
    if "answer_relevance" in metrics:
        scores.append(await answer_relevance(case["question"], result.answer))
    return scores


async def evaluate_turn(payload: dict[str, Any]) -> dict[str, Any]:
    """Online evaluation of a sampled live turn -- no gold answer available."""
    contexts = [c.get("section", "") for c in payload.get("citations", [])]
    scores = [
        await faithfulness(payload["answer"], contexts),
        await answer_relevance(payload["question"], payload["answer"]),
        citation_coverage(payload["answer"], payload.get("citations", [])),
    ]

    run_id = uuid.uuid4()
    async with session_scope() as session:
        session.add(
            EvalRun(
                id=run_id, name="online-sample", mode="online", status="passed",
                total_cases=1,
                passed_cases=int(all(s.passed for s in scores)),
                aggregate_scores={s.metric: s.score for s in scores},
            )
        )
        for score in scores:
            session.add(
                EvalResult(
                    run_id=run_id,
                    case_id=payload.get("session_id", "unknown"),
                    metric=score.metric,
                    score=score.score,
                    passed=score.passed,
                    question=payload["question"],
                    prediction=payload["answer"],
                    detail=score.detail,
                )
            )

    return {"run_id": str(run_id), "scores": {s.metric: s.score for s in scores}}
