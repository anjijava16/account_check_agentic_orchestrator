"""Shared pytest fixtures."""
from __future__ import annotations

import pytest

from app.security.auth import Principal


@pytest.fixture
def customer_principal() -> Principal:
    return Principal(
        subject="alex.rivera",
        tenant_id="default",
        roles=["customer"],
        scopes=["chat:write", "documents:read", "documents:write"],
        customer_ids=["CUST-1001"],
    )


@pytest.fixture
def operator_principal() -> Principal:
    return Principal(
        subject="ops.user",
        tenant_id="default",
        roles=["agent_operator"],
        scopes=["chat:write", "documents:read", "documents:write"],
        customer_ids=[],
    )
