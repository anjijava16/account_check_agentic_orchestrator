"""Service MCP Server.

Backs the Service Agent: address changes, cheque book requests, KYC updates,
and knowledge-base retrieval over the OpenSearch index.

Every mutating tool here returns `requires_approval: true` and does *not*
commit the change. Commitment happens only after the human-in-the-loop gate
resolves and the orchestrator calls `confirm_service_request`. This split is
deliberate: the model can propose, but it cannot mutate a customer record on
its own authority.

Run standalone:  python -m app.mcp.servers.service_server
"""
from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from app.core.logging import configure_logging, get_logger
from app.mcp.servers import core_banking

log = get_logger(__name__)
mcp = FastMCP(
    name="service",
    instructions=(
        "Servicing actions and policy lookups. Mutating tools stage a request for human "
        "approval -- they never apply a change directly. Always quote the returned reference "
        "back to the customer."
    ),
)

_PENDING: dict[str, dict[str, Any]] = {}


@mcp.tool
def update_address(
    customer_id: Annotated[str, Field(description="Verified customer identifier")],
    line1: Annotated[str, Field(description="Street address line 1")],
    city: Annotated[str, Field(description="City")],
    postcode: Annotated[str, Field(description="Postal / ZIP code")],
    country: Annotated[str, Field(description="ISO country code, e.g. US")] = "US",
    line2: Annotated[str | None, Field(description="Apartment, suite, etc.")] = None,
    state: Annotated[str | None, Field(description="State or province")] = None,
) -> dict[str, Any]:
    """Stage a change of registered address for approval.

    This does NOT change the address. It validates the input, computes a diff
    against the record on file, and returns a reference plus the approval
    requirement. Tell the customer their address has been *submitted*.
    """
    customer = core_banking.get_customer(customer_id)
    if customer is None:
        return {"error": "customer_not_found"}

    proposed = {
        "line1": line1.strip(),
        "line2": (line2 or "").strip() or None,
        "city": city.strip(),
        "state": (state or "").strip() or None,
        "postcode": postcode.strip().upper(),
        "country": country.strip().upper(),
    }
    current = customer["address"]
    diff = {k: {"from": current.get(k), "to": v} for k, v in proposed.items() if current.get(k) != v}
    if not diff:
        return {"status": "no_change", "message": "Submitted address matches the record on file."}

    record = core_banking.create_service_request(customer_id, "change_of_address", proposed)
    _PENDING[record["reference"]] = {"type": "change_of_address", "payload": proposed,
                                     "customer_id": customer_id}
    return {
        "reference": record["reference"],
        "status": "pending_approval",
        "requires_approval": True,
        "changes": diff,
        "sla_days": record["sla_days"],
        "note": "A colleague will verify this change before it takes effect.",
    }


@mcp.tool
def request_cheque_book(
    customer_id: Annotated[str, Field(description="Verified customer identifier")],
    account_id: Annotated[str, Field(description="Account the cheque book is for")],
    leaves: Annotated[int, Field(description="Number of leaves: 25, 50 or 100", ge=25, le=100)] = 50,
    delivery: Annotated[str, Field(description="branch|post")] = "post",
) -> dict[str, Any]:
    """Stage a cheque book order for approval.

    Only current accounts are eligible. Returns a reference and the fee that
    applies so the customer is told the cost before it is charged.
    """
    account = core_banking.get_account(customer_id, account_id)
    if account is None:
        return {"error": "account_not_found_for_customer"}
    if account["type"] != "current":
        return {"error": "ineligible_account_type", "account_type": account["type"]}
    if leaves not in (25, 50, 100):
        return {"error": "invalid_leaf_count", "allowed": [25, 50, 100]}

    fee = {25: 0.0, 50: 5.00, 100: 8.50}[leaves]
    record = core_banking.create_service_request(
        customer_id,
        "cheque_book",
        {"account_id": account_id, "leaves": leaves, "delivery": delivery, "fee_usd": fee},
    )
    _PENDING[record["reference"]] = {"type": "cheque_book", "payload": record["payload"],
                                     "customer_id": customer_id}
    return {
        "reference": record["reference"],
        "status": "pending_approval",
        "requires_approval": True,
        "leaves": leaves,
        "fee_usd": fee,
        "delivery": delivery,
        "expected_days": record["sla_days"],
    }


@mcp.tool
def start_kyc_update(
    customer_id: Annotated[str, Field(description="Verified customer identifier")],
    reason: Annotated[
        str, Field(description="periodic_review|address_change|document_expiry|customer_request")
    ],
    document_type: Annotated[
        str | None, Field(description="passport|driving_licence|national_id")
    ] = None,
) -> dict[str, Any]:
    """Open a KYC refresh case and return the document checklist.

    High risk: always staged for approval. Never tell a customer their KYC is
    complete from this tool's response -- it only opens the case.
    """
    customer = core_banking.get_customer(customer_id)
    if customer is None:
        return {"error": "customer_not_found"}

    checklist = ["Government photo ID (unexpired)", "Proof of address dated within 3 months"]
    if reason == "document_expiry":
        checklist.append("Replacement for the expired document on file")
    if customer.get("segment") == "premier":
        checklist.append("Source-of-wealth declaration")

    record = core_banking.create_service_request(
        customer_id, "kyc_update", {"reason": reason, "document_type": document_type}
    )
    _PENDING[record["reference"]] = {"type": "kyc_update", "payload": record["payload"],
                                     "customer_id": customer_id}
    return {
        "reference": record["reference"],
        "status": "pending_approval",
        "requires_approval": True,
        "current_kyc_status": customer["kyc_status"],
        "last_reviewed": customer["kyc_last_reviewed"],
        "required_documents": checklist,
        "expected_days": record["sla_days"],
    }


@mcp.tool
def confirm_service_request(
    reference: Annotated[str, Field(description="Reference returned by a staging tool")],
    approved_by: Annotated[str, Field(description="Identifier of the approving human")],
) -> dict[str, Any]:
    """Commit a previously staged request after human approval.

    The orchestrator calls this only once an ApprovalRequest row has been
    decided. It is never exposed to the customer-facing agent.
    """
    pending = _PENDING.pop(reference, None)
    if pending is None:
        return {"error": "unknown_or_already_applied", "reference": reference}

    applied: dict[str, Any] = {"reference": reference, "approved_by": approved_by}
    if pending["type"] == "change_of_address":
        applied["result"] = core_banking.apply_address_change(
            pending["customer_id"], pending["payload"]
        )
    else:
        applied["result"] = {"status": "queued_for_fulfilment", **pending["payload"]}
    applied["status"] = "applied"
    log.info("mcp.service_request_applied", reference=reference, type=pending["type"])
    return applied


@mcp.tool
async def search_knowledge_base(
    query: Annotated[str, Field(description="Natural-language policy or product question")],
    tenant_id: Annotated[str, Field(description="Tenant scope for the search")] = "default",
    top_k: Annotated[int, Field(description="Number of passages to return", ge=1, le=20)] = 5,
    doc_type: Annotated[
        str | None, Field(description="Filter: policy|product|faq|terms|procedure")
    ] = None,
) -> dict[str, Any]:
    """Hybrid search over the bank's ingested knowledge base.

    Returns compressed passages with citations. Answer policy questions ONLY
    from these passages -- if they do not contain the answer, say so and offer
    to route the customer to a colleague.
    """
    from app.llm.gateway import get_gateway
    from app.vector.hybrid_search import HybridSearcher, SearchRequest

    try:
        vectors, _ = await get_gateway().embed([query])
        embedding = vectors[0]
    except Exception as exc:  # noqa: BLE001
        log.warning("mcp.kb_embed_failed", error=str(exc))
        embedding = None

    request = SearchRequest(
        query=query,
        embedding=embedding,
        tenant_id=tenant_id,
        top_k=top_k,
        filters={"doc_type": doc_type} if doc_type else {},
        strategy="hybrid" if embedding else "bm25",
    )
    hits = await HybridSearcher().search(request)
    return {
        "query": query,
        "returned": len(hits),
        "passages": [
            {
                "content": h.content,
                "citation": h.citation(),
                "score": round(h.rerank_score if h.rerank_score is not None else h.score, 4),
            }
            for h in hits
        ],
    }


@mcp.tool
def get_request_status(
    reference: Annotated[str, Field(description="Service request reference, e.g. SR-1A2B3C4D5E")],
) -> dict[str, Any]:
    """Look up the status of any previously raised service request."""
    record = core_banking.get_service_request(reference)
    if record is None:
        return {"error": "reference_not_found", "reference": reference}
    return {
        "reference": reference,
        "type": record["type"],
        "status": record["status"],
        "submitted_at": record["submitted_at"],
        "sla_days": record["sla_days"],
    }


if __name__ == "__main__":
    configure_logging()
    log.info("mcp.service_server_starting", port=8083)
    asyncio.run(
        mcp.run_async(transport="http", host="0.0.0.0", port=8083, path="/mcp")  # noqa: S104
    )
