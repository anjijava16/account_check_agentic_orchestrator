#!/usr/bin/env python3
"""Run the offline evaluation suite and exit non-zero on regression.

Wire this into CI as a deploy gate.
"""
from __future__ import annotations

import asyncio
import sys
import uuid

from app.core.config import settings
from app.core.logging import configure_logging
from app.db.session import dispose_engine
from app.evaluation.runner import run_offline_suite
from app.memory.redis_client import close_redis
from app.vector.opensearch_client import close_client


async def main() -> int:
    configure_logging(json_output=False)
    result = await run_offline_suite(
        run_id=uuid.uuid4(),
        name="ci-offline-suite",
        dataset_path=settings.eval_dataset_path,
        metrics=[
            "routing_accuracy",
            "tool_correctness",
            "citation_coverage",
            "context_precision",
            "answer_similarity",
            "faithfulness",
            "answer_relevance",
        ],
    )

    print(f"\nRun {result['run_id']} -> {result['status']}\n")
    for metric, score in sorted(result.get("aggregates", {}).items()):
        print(f"  {metric:<22} {score:.4f}")
    if result.get("regressions"):
        print(f"\nRegressions: {', '.join(result['regressions'])}")

    await close_client()
    await close_redis()
    await dispose_engine()
    return 1 if result["status"] == "failed" else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
