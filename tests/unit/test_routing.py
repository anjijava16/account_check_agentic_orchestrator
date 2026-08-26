"""The coordinator's keyword fast-path is pure logic, so it's cheap to pin."""
from __future__ import annotations

import pytest

from app.agents.nodes.coordinator import _fast_route
from app.core.constants import INTENT_TO_AGENT, AgentName, Intent


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("What's my balance?", Intent.BALANCE_ENQUIRY),
        ("how much do i have in savings", Intent.BALANCE_ENQUIRY),
        ("Send me a statement", Intent.STATEMENT_REQUEST),
        ("I need a cheque book", Intent.CHEQUE_BOOK_REQUEST),
        ("i need a new checkbook", Intent.CHEQUE_BOOK_REQUEST),
        ("update my address please", Intent.CHANGE_OF_ADDRESS),
        ("I need to do a KYC update", Intent.KYC_UPDATE),
        ("what did I spend at Amazon", Intent.TRANSACTION_DETAILS),
        ("hello there", Intent.SMALL_TALK),
    ],
)
def test_fast_path_routes(text, expected):
    assert _fast_route(text) == expected


def test_ambiguous_text_falls_through_to_model():
    assert _fast_route("Tell me about the thing from yesterday") is None


def test_every_intent_maps_to_an_agent():
    for intent in Intent:
        assert intent in INTENT_TO_AGENT


def test_service_owns_the_mutating_intents():
    for intent in (Intent.CHANGE_OF_ADDRESS, Intent.KYC_UPDATE, Intent.CHEQUE_BOOK_REQUEST):
        assert INTENT_TO_AGENT[intent] == AgentName.SERVICE
