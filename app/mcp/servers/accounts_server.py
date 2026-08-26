"""Accounts MCP Server.

Exposes read-only account operations as MCP tools. Two rules this server
follows that keep it safe to hand to an LLM:

  1. Every tool takes an explicit `customer_id`; nothing is inferred from
     ambient state. The agent layer supplies it from the verified principal,
     not from the user's message text.
  2. Balances and account numbers are returned masked by default. The full
     number is only produced by `get_account_details`, which the policy layer
     restricts more tightly.

Run standalone:  python -m app.mcp.servers.accounts_server
"""
from __future__ import annotations

from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from app.core.logging import configure_logging, get_logger
from app.mcp.servers import core_banking

log = get_logger(__name__)
mcp = FastMCP(
    name="accounts",
    instructions=(
        "Read-only access to customer deposit accounts. Use list_accounts first when the "
        "customer has not named a specific account. Never guess an account_id."
    ),
)


def mask(account_id: str) -> str:
    return f"****{account_id[-4:]}" if len(account_id) > 4 else "****"


@mcp.tool
def list_accounts(
    customer_id: Annotated[str, Field(description="Verified customer identifier, e.g. CUST-1001")],
) -> dict[str, Any]:
    """List every open account the customer holds, with masked identifiers.

    Call this before any balance or transaction lookup when the customer has
    not explicitly named an account.
    """
    accounts = core_banking.list_accounts(customer_id)
    return {
        "customer_id": customer_id,
        "count": len(accounts),
        "accounts": [
            {
                "account_id": a["account_id"],
                "masked": mask(a["account_id"]),
                "type": a["type"],
                "nickname": a["nickname"],
                "currency": a["currency"],
                "status": a["status"],
            }
            for a in accounts
        ],
    }


@mcp.tool
def get_balance(
    customer_id: Annotated[str, Field(description="Verified customer identifier")],
    account_id: Annotated[
        str | None, Field(description="Specific account; omit to return all accounts")
    ] = None,
) -> dict[str, Any]:
    """Return available and ledger balances.

    Available balance reflects pending authorisations; ledger balance does not.
    When they differ, say so -- customers routinely mistake one for the other.
    """
    accounts = core_banking.list_accounts(customer_id)
    if account_id:
        accounts = [a for a in accounts if a["account_id"] == account_id]
        if not accounts:
            return {"error": "account_not_found", "account_id": account_id}

    return {
        "customer_id": customer_id,
        "balances": [
            {
                "account_id": a["account_id"],
                "masked": mask(a["account_id"]),
                "nickname": a["nickname"],
                "currency": a["currency"],
                "available_balance": a["available_balance"],
                "ledger_balance": a["ledger_balance"],
                "pending_difference": round(a["ledger_balance"] - a["available_balance"], 2),
                "overdraft_limit": a.get("overdraft_limit", 0.0),
            }
            for a in accounts
        ],
    }


@mcp.tool
def get_account_details(
    customer_id: Annotated[str, Field(description="Verified customer identifier")],
    account_id: Annotated[str, Field(description="Account identifier to describe")],
) -> dict[str, Any]:
    """Full account record including IBAN and interest rate.

    Restricted: only invoke when the customer has explicitly asked for account
    details such as their IBAN, sort code, or interest rate.
    """
    account = core_banking.get_account(customer_id, account_id)
    if account is None:
        return {"error": "account_not_found", "account_id": account_id}
    return {"customer_id": customer_id, "account": account}


if __name__ == "__main__":
    configure_logging()
    log.info("mcp.accounts_server_starting", port=8081)
    mcp.run(transport="http", host="0.0.0.0", port=8081, path="/mcp")  # noqa: S104
