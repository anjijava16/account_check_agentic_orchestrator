"""Transactions MCP Server.

Backs the Transaction Agent: search, detail lookup, and statement generation.
Search is deliberately parameter-rich so the model can express a precise query
instead of pulling 400 rows and filtering in the prompt -- that difference is
worth thousands of tokens per turn.

Run standalone:  python -m app.mcp.servers.transactions_server
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from app.core.logging import configure_logging, get_logger
from app.mcp.servers import core_banking

log = get_logger(__name__)
mcp = FastMCP(
    name="transactions",
    instructions=(
        "Search and explain account transactions. Prefer narrow date ranges. "
        "Always report amounts with their currency and state whether a row is a debit or credit."
    ),
)


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC).date()
    except ValueError:
        return None


@mcp.tool
def search_transactions(
    customer_id: Annotated[str, Field(description="Verified customer identifier")],
    account_id: Annotated[str, Field(description="Account to search")],
    start_date: Annotated[str | None, Field(description="ISO date YYYY-MM-DD")] = None,
    end_date: Annotated[str | None, Field(description="ISO date YYYY-MM-DD")] = None,
    merchant: Annotated[str | None, Field(description="Merchant name substring")] = None,
    category: Annotated[
        str | None,
        Field(description="groceries|fuel|utilities|transport|shopping|dining|telecoms|income"),
    ] = None,
    min_amount: Annotated[float | None, Field(description="Minimum absolute amount")] = None,
    max_amount: Annotated[float | None, Field(description="Maximum absolute amount")] = None,
    limit: Annotated[int, Field(description="Max rows, 1-100", ge=1, le=100)] = 20,
) -> dict[str, Any]:
    """Search posted transactions with server-side filtering.

    Returns rows newest-first plus a summary (totals in/out, top categories) so
    a single call answers "what did I spend on groceries last month".
    """
    if core_banking.get_account(customer_id, account_id) is None:
        return {"error": "account_not_found_for_customer", "account_id": account_id}

    rows = core_banking.get_transactions(
        account_id,
        start=_parse_date(start_date),
        end=_parse_date(end_date),
        merchant=merchant,
        category=category,
        min_amount=min_amount,
        max_amount=max_amount,
        limit=limit,
    )

    debits = sum(r["amount"] for r in rows if r["direction"] == "debit")
    credits = sum(r["amount"] for r in rows if r["direction"] == "credit")
    by_category: dict[str, float] = {}
    for row in rows:
        if row["direction"] == "debit":
            by_category[row["category"]] = round(
                by_category.get(row["category"], 0.0) + row["amount"], 2
            )

    return {
        "account_id": account_id,
        "returned": len(rows),
        "summary": {
            "total_out": round(debits, 2),
            "total_in": round(credits, 2),
            "net": round(credits - debits, 2),
            "top_categories": sorted(by_category.items(), key=lambda kv: kv[1], reverse=True)[:5],
        },
        "transactions": rows,
    }


@mcp.tool
def get_transaction(
    customer_id: Annotated[str, Field(description="Verified customer identifier")],
    account_id: Annotated[str, Field(description="Account the transaction belongs to")],
    transaction_id: Annotated[str, Field(description="Transaction identifier, e.g. TXN-00A1B2C3D4")],
) -> dict[str, Any]:
    """Fetch one transaction in full, including channel and running balance.

    Use this when a customer disputes or queries a specific line item.
    """
    if core_banking.get_account(customer_id, account_id) is None:
        return {"error": "account_not_found_for_customer"}
    row = core_banking.get_transaction(account_id, transaction_id)
    if row is None:
        return {"error": "transaction_not_found", "transaction_id": transaction_id}
    return {"transaction": row}


@mcp.tool
def request_statement(
    customer_id: Annotated[str, Field(description="Verified customer identifier")],
    account_id: Annotated[str, Field(description="Account to produce a statement for")],
    period_start: Annotated[str, Field(description="ISO date YYYY-MM-DD")],
    period_end: Annotated[str, Field(description="ISO date YYYY-MM-DD")],
    delivery: Annotated[str, Field(description="email|post|download")] = "download",
) -> dict[str, Any]:
    """Queue a statement for a date range and return a tracking reference.

    Statements are generated asynchronously; tell the customer the reference
    and the expected turnaround rather than implying it is ready immediately.
    """
    start, end = _parse_date(period_start), _parse_date(period_end)
    if start is None or end is None:
        return {"error": "invalid_date_format", "expected": "YYYY-MM-DD"}
    if end < start:
        return {"error": "end_before_start"}
    if (end - start) > timedelta(days=400):
        return {"error": "period_too_long", "max_days": 400}

    record = core_banking.create_service_request(
        customer_id,
        "statement",
        {
            "account_id": account_id,
            "period_start": period_start,
            "period_end": period_end,
            "delivery": delivery,
        },
    )
    return {
        "reference": record["reference"],
        "status": record["status"],
        "delivery": delivery,
        "expected_within_days": 1,
    }


if __name__ == "__main__":
    configure_logging()
    log.info("mcp.transactions_server_starting", port=8082)
    mcp.run(transport="http", host="0.0.0.0", port=8082, path="/mcp")  # noqa: S104
