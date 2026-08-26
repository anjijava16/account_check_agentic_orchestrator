from __future__ import annotations

import pytest

from app.core.exceptions import AuthorizationError
from app.security.authz import PolicyEngine, enforce_tool


def test_customer_can_read_own_balance(customer_principal):
    engine = PolicyEngine()
    assert engine.check_tool(customer_principal, "accounts__get_balance").allowed


def test_customer_cannot_call_commit_tool(customer_principal):
    engine = PolicyEngine()
    assert not engine.check_tool(customer_principal, "service__confirm_service_request").allowed


def test_operator_has_wildcard_access(operator_principal):
    engine = PolicyEngine()
    assert engine.check_tool(operator_principal, "service__confirm_service_request").allowed


def test_ownership_blocks_other_customers(customer_principal):
    with pytest.raises(AuthorizationError):
        enforce_tool(customer_principal, "accounts__get_balance", {"customer_id": "CUST-9999"})


def test_ownership_allows_own_customer_id(customer_principal):
    enforce_tool(customer_principal, "accounts__get_balance", {"customer_id": "CUST-1001"})


def test_high_risk_intent_requires_approval(customer_principal):
    decision = PolicyEngine().check_intent(customer_principal, "kyc_update")
    assert decision.allowed
    assert decision.requires_approval


def test_read_intent_needs_no_approval(customer_principal):
    decision = PolicyEngine().check_intent(customer_principal, "balance_enquiry")
    assert decision.allowed
    assert not decision.requires_approval


def test_classification_ceiling_enforced(customer_principal):
    engine = PolicyEngine()
    assert engine.check_classification(customer_principal, "confidential").allowed
    assert not engine.check_classification(customer_principal, "restricted").allowed
