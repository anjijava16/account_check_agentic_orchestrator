"""MCP tool functions are plain callables, so they test like any other code."""
from __future__ import annotations

from app.mcp.servers import core_banking


def test_list_accounts_returns_known_customer():
    accounts = core_banking.list_accounts("CUST-1001")
    assert len(accounts) == 2
    assert {a["type"] for a in accounts} == {"current", "savings"}


def test_unknown_customer_returns_empty():
    assert core_banking.list_accounts("CUST-0000") == []


def test_transactions_are_deterministic():
    a = core_banking.get_transactions("ACC-90014455", limit=10)
    b = core_banking.get_transactions("ACC-90014455", limit=10)
    assert [t["transaction_id"] for t in a] == [t["transaction_id"] for t in b]


def test_transactions_respect_category_filter():
    rows = core_banking.get_transactions("ACC-90014455", category="groceries", limit=50)
    assert all(r["category"] == "groceries" for r in rows)


def test_transactions_respect_amount_bounds():
    rows = core_banking.get_transactions("ACC-90014455", min_amount=100, limit=50)
    assert all(r["amount"] >= 100 for r in rows)


def test_transactions_sorted_newest_first():
    rows = core_banking.get_transactions("ACC-90014455", limit=20)
    dates = [r["posted_date"] for r in rows]
    assert dates == sorted(dates, reverse=True)


def test_service_request_creates_reference():
    record = core_banking.create_service_request("CUST-1001", "cheque_book", {"leaves": 50})
    assert record["reference"].startswith("SR-")
    assert core_banking.get_service_request(record["reference"]) is not None


def test_address_change_returns_before_and_after():
    result = core_banking.apply_address_change("CUST-1001", {"city": "Pittsburgh"})
    assert result["previous"]["city"] != "Pittsburgh"
    assert result["current"]["city"] == "Pittsburgh"
    # Restore so other tests see the original fixture.
    core_banking.apply_address_change("CUST-1001", {"city": "Philadelphia"})
