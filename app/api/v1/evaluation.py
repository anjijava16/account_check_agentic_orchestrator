"""EvaluationRouter -- the Agent Evaluation Suite's HTTP surface."""
from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import PrincipalDep, require_role
from app.core.config import settings
from app.core.exceptions import NotFoundError
from app.db.models import EvalResult, EvalRun
from app.db.session import session_scope

router = APIRouter(
    prefix="/evaluation",
    tags=["evaluation"],
    dependencies=[Depends(require_role("agent_operator"))],
)


class RunEvalRequest(BaseModel):
    name: str = Field(default="offline-suite", max_length=128)
    dataset_path: str | None = None
    metrics: list[str] = Field(
        default_factory=lambda: [
            "routing_accuracy",
            "faithfulness",
            "answer_relevance",
            "context_precision",
            "citation_coverage",
            "tool_correctness",
        ]
    )


@router.post("/runs", summary="Start an offline evaluation run")
async def start_run(
    payload: RunEvalRequest, background: BackgroundTasks, principal: PrincipalDep
) -> dict[str, Any]:
    """Kicks off the golden-dataset suite in the background and returns the run
    id immediately. Poll the run endpoint for aggregate scores."""
    from app.evaluation.runner import run_offline_suite

    run_id = uuid.uuid4()
    background.add_task(
        run_offline_suite,
        run_id=run_id,
        name=payload.name,
        dataset_path=payload.dataset_path or settings.eval_dataset_path,
        metrics=payload.metrics,
        tenant_id=principal.tenant_id,
    )
    return {"run_id": str(run_id), "status": "started", "metrics": payload.metrics}


@router.get("/runs", summary="List evaluation runs")
async def list_runs(limit: Annotated[int, Query(ge=1, le=100)] = 20) -> list[dict[str, Any]]:
    async with session_scope() as session:
        rows = (
            await session.execute(select(EvalRun).order_by(EvalRun.created_at.desc()).limit(limit))
        ).scalars().all()
    return [
        {
            "run_id": str(r.id),
            "name": r.name,
            "mode": r.mode,
            "status": r.status,
            "total_cases": r.total_cases,
            "passed_cases": r.passed_cases,
            "pass_rate": round(r.passed_cases / r.total_cases, 4) if r.total_cases else None,
            "aggregate_scores": r.aggregate_scores,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


@router.get("/runs/{run_id}", summary="Run detail with per-case results")
async def get_run(run_id: uuid.UUID) -> dict[str, Any]:
    async with session_scope() as session:
        run = (
            await session.execute(select(EvalRun).where(EvalRun.id == run_id))
        ).scalar_one_or_none()
        if run is None:
            raise NotFoundError("Evaluation run not found")
        results = (
            await session.execute(select(EvalResult).where(EvalResult.run_id == run_id))
        ).scalars().all()

    by_metric: dict[str, list[float]] = {}
    for r in results:
        by_metric.setdefault(r.metric, []).append(r.score)

    return {
        "run_id": str(run_id),
        "name": run.name,
        "status": run.status,
        "total_cases": run.total_cases,
        "passed_cases": run.passed_cases,
        "aggregate_scores": run.aggregate_scores,
        "metric_means": {
            m: round(sum(v) / len(v), 4) for m, v in by_metric.items() if v
        },
        "failures": [
            {
                "case_id": r.case_id,
                "metric": r.metric,
                "score": r.score,
                "question": r.question,
                "prediction": r.prediction,
                "reference": r.reference,
                "detail": r.detail,
            }
            for r in results
            if not r.passed
        ][:50],
    }
