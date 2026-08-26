"""Simulated core-banking backend.

Stands in for the bank's actual systems of record. Every MCP server in this
project talks to this module instead of directly to a database, which is the
shape you want in production too: the MCP layer is a *protocol adapter*, not a
place to put business logic.

Swap the internals for real HTTP/gRPC calls to core banking and nothing above
this line changes.
"""
from __future__ import annotations

import random
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

_ACCOUNTS: dict[str, list[dict[str, Any]]] = {
    "CUST-1001": [
        {
            "account_id": "ACC-90014455",
            "type": "current",
            "nickname": "Everyday Current",
            "currency": "USD",
            "available_balance": 4821.37,
            "ledger_balance": 4996.12,
            "overdraft_limit": 500.00,
            "status": "active",
            "opened_on": "2019-04-12",
            "iban": "US33BANK00090014455",
        },
        {
            "account_id": "ACC-90014456",
            "type": "savings",
            "nickname": "Rainy Day",
            "currency": "USD",
            "available_balance": 18240.00,
            "ledger_balance": 18240.00,
            "overdraft_limit": 0.0,
            "interest_rate_apy": 3.85,
            "status": "active",
            "opened_on": "2020-11-02",
            "iban": "US33BANK00090014456",
        },
    ]
}

_MERCHANTS = [
    ("Whole Foods Market", "groceries"),
    ("Shell Service Station", "fuel"),
    ("Con Edison", "utilities"),
    ("SEPTA Transit", "transport"),
    ("Amazon.com", "shopping"),
    ("Blue Bottle Coffee", "dining"),
    ("Verizon Wireless", "telecoms"),
    ("Payroll Credit", "income"),
]

_CUSTOMERS: dict[str, dict[str, Any]] = {
    "CUST-1001": {
        "customer_id": "CUST-1001",
        "full_name": "Alex Rivera",
        "email": "alex.rivera@example.com",
        "phone": "+1 215 555 0142",
        "address": {
            "line1": "1420 Walnut Street",
            "line2": "Apt 8B",
            "city": "Philadelphia",
            "state": "PA",
            "postcode": "19102",
            "country": "US",
        },
        "kyc_status": "verified",
        "kyc_last_reviewed": "2024-06-18",
        "segment": "premier",
    }
}

_SERVICE_REQUESTS: dict[str, dict[str, Any]] = {}


def _rng(seed: str) -> random.Random:
    return random.Random(seed)


def list_accounts(customer_id: str) -> list[dict[str, Any]]:
    return _ACCOUNTS.get(customer_id, [])


def get_account(customer_id: str, account_id: str) -> dict[str, Any] | None:
    return next(
        (a for a in _ACCOUNTS.get(customer_id, []) if a["account_id"] == account_id), None
    )


def get_transactions(
    account_id: str,
    *,
    start: date | None = None,
    end: date | None = None,
    min_amount: float | None = None,
    max_amount: float | None = None,
    merchant: str | None = None,
    category: str | None = None,
    limit: int = 25,
) -> list[dict[str, Any]]:
    """Deterministic synthetic ledger so demos and tests are reproducible."""
    rng = _rng(account_id)
    end = end or datetime.now(UTC).date()
    start = start or (end - timedelta(days=90))
    rows: list[dict[str, Any]] = []
    balance = 5200.00
    cursor = end

    while cursor >= start and len(rows) < 400:
        for _ in range(rng.randint(0, 3)):
            name, cat = rng.choice(_MERCHANTS)
            credit = cat == "income"
            amount = round(rng.uniform(1200, 3400) if credit else rng.uniform(3.5, 480.0), 2)
            balance = round(balance + amount if credit else balance - amount, 2)
            rows.append(
                {
                    "transaction_id": f"TXN-{rng.getrandbits(40):010X}",
                    "account_id": account_id,
                    "posted_date": cursor.isoformat(),
                    "value_date": cursor.isoformat(),
                    "description": name,
                    "merchant": name,
                    "category": cat,
                    "direction": "credit" if credit else "debit",
                    "amount": amount,
                    "currency": "USD",
                    "running_balance": balance,
                    "channel": rng.choice(["card", "ach", "wire", "mobile"]),
                    "status": "posted",
                }
            )
        cursor -= timedelta(days=1)

    def keep(row: dict[str, Any]) -> bool:
        if min_amount is not None and row["amount"] < min_amount:
            return False
        if max_amount is not None and row["amount"] > max_amount:
            return False
        if merchant and merchant.lower() not in row["merchant"].lower():
            return False
        if category and category.lower() != row["category"]:
            return False
        return True

    filtered = [r for r in rows if keep(r)]
    filtered.sort(key=lambda r: r["posted_date"], reverse=True)
    return filtered[:limit]


def get_transaction(account_id: str, transaction_id: str) -> dict[str, Any] | None:
    for row in get_transactions(account_id, limit=400):
        if row["transaction_id"] == transaction_id:
            return row
    return None


def get_customer(customer_id: str) -> dict[str, Any] | None:
    return _CUSTOMERS.get(customer_id)


def create_service_request(
    customer_id: str, request_type: str, payload: dict[str, Any]
) -> dict[str, Any]:
    reference = f"SR-{uuid.uuid4().hex[:10].upper()}"
    record = {
        "reference": reference,
        "customer_id": customer_id,
        "type": request_type,
        "payload": payload,
        "status": "submitted",
        "submitted_at": datetime.now(UTC).isoformat(),
        "sla_days": {"change_of_address": 2, "cheque_book": 5, "kyc_update": 7}.get(
            request_type, 5
        ),
    }
    _SERVICE_REQUESTS[reference] = record
    return record


def get_service_request(reference: str) -> dict[str, Any] | None:
    return _SERVICE_REQUESTS.get(reference)


def apply_address_change(customer_id: str, address: dict[str, Any]) -> dict[str, Any]:
    customer = _CUSTOMERS.get(customer_id)
    if customer is None:
        raise KeyError(customer_id)
    previous = dict(customer["address"])
    customer["address"] = {**previous, **address}
    return {"previous": previous, "current": customer["address"]}
