from __future__ import annotations

from app.evaluation.metrics import (
    answer_similarity,
    citation_coverage,
    context_precision,
    routing_accuracy,
    tool_correctness,
)


def test_routing_accuracy_exact_match():
    assert routing_accuracy("balance_enquiry", "balance_enquiry").score == 1.0


def test_routing_accuracy_mismatch():
    assert routing_accuracy("small_talk", "balance_enquiry").score == 0.0


def test_tool_correctness_full_match():
    score = tool_correctness(["accounts__get_balance"], ["get_balance"])
    assert score.score == 1.0
    assert score.passed


def test_tool_correctness_partial():
    score = tool_correctness(["accounts__get_balance"], ["get_balance", "list_accounts"])
    assert score.score == 0.5
    assert "list_accounts" in score.detail["missing"]


def test_tool_correctness_no_expectation_passes():
    assert tool_correctness([], []).passed


def test_citation_coverage_requires_citation_for_claims():
    assert citation_coverage("The limit is 500 USD per day", []).score == 0.0
    assert citation_coverage("The limit is 500 USD per day", [{"chunk_id": "x"}]).score == 1.0


def test_citation_coverage_ignores_claimless_answers():
    assert citation_coverage("Hello, how can I help?", []).passed


def test_context_precision_penalises_irrelevant_chunks():
    good = context_precision(["overdraft fees apply monthly"], "overdraft fees apply monthly")
    bad = context_precision(["completely unrelated text here"], "overdraft fees apply monthly")
    assert good.score > bad.score


def test_answer_similarity_symmetric_on_identical_text():
    score = answer_similarity("your balance is 4821 dollars", "your balance is 4821 dollars")
    assert score.score == 1.0
