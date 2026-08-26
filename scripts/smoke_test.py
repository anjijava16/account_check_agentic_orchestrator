#!/usr/bin/env python3
"""End-to-end smoke test against a running stack.

Exercises the real request path: mint a token, upload a document, wait for
ingestion, then run one question per intent through the chat endpoint and
assert the routing landed where it should.
"""
from __future__ import annotations

import asyncio
import io
import sys
import time

import httpx
from jose import jwt

from app.core.config import settings

BASE = "http://localhost:8000"
API = f"{BASE}{settings.api_prefix}"

CASES = [
    ("What's my balance?", "balance_enquiry"),
    ("What did I spend on groceries in the last 30 days?", "transaction_details"),
    ("Can I get a statement for last month?", "statement_request"),
    ("How long does an international transfer take?", "knowledge_lookup"),
    ("I need a new cheque book", "cheque_book_request"),
    ("What's the weather today?", "out_of_scope"),
]


def token(roles: list[str]) -> str:
    return jwt.encode(
        {
            "sub": "smoke.tester",
            "tenant_id": "default",
            "customer_ids": ["CUST-1001"],
            "roles": roles,
            "scopes": ["chat:write", "documents:read", "documents:write"],
        },
        settings.dev_shared_secret,
        algorithm="HS256",
    )


async def main() -> int:
    headers = {"Authorization": f"Bearer {token(['customer', 'agent_operator'])}"}
    failures: list[str] = []

    async with httpx.AsyncClient(timeout=90.0) as client:
        print("→ health")
        health = await client.get(f"{BASE}/health")
        print(f"  {health.status_code} {health.json().get('status')}")
        for component in health.json().get("components", []):
            marker = "ok" if component["status"] == "up" else "DOWN"
            print(f"    {component['name']:<20} {marker}")

        print("\n→ upload a document")
        content = b"# Smoke Test Policy\n\nThe smoke test withdrawal limit is 250 USD per day.\n"
        upload = await client.post(
            f"{API}/ingestion/documents",
            headers=headers,
            files={"file": ("smoke.md", io.BytesIO(content), "text/markdown")},
            data={"doc_type": "policy", "classification": "internal", "tags": "smoke"},
        )
        if upload.status_code not in (200, 202):
            failures.append(f"upload returned {upload.status_code}: {upload.text[:200]}")
        else:
            document_id = upload.json()["document_id"]
            print(f"  queued {document_id}")

            print("→ waiting for ingestion")
            for _ in range(30):
                await asyncio.sleep(2)
                status = await client.get(
                    f"{API}/ingestion/documents/{document_id}/status", headers=headers
                )
                state = status.json()["status"]
                if state in ("completed", "failed", "quarantined"):
                    print(f"  {state} ({status.json()['indexed_count']} chunks indexed)")
                    if state != "completed":
                        failures.append(f"ingestion ended in {state}")
                    break
            else:
                failures.append("ingestion did not finish within 60s")

        print("\n→ chat turns")
        session_id = None
        for question, expected_intent in CASES:
            started = time.perf_counter()
            response = await client.post(
                f"{API}/chat",
                headers=headers,
                json={"message": question, "session_id": session_id},
            )
            elapsed = int((time.perf_counter() - started) * 1000)
            if response.status_code != 200:
                failures.append(f"chat failed for '{question}': {response.status_code}")
                print(f"  ✗ {question[:45]:<45} HTTP {response.status_code}")
                continue
            body = response.json()
            session_id = body["session_id"]
            actual = body.get("intent")
            ok = actual == expected_intent
            if not ok:
                failures.append(f"'{question}' routed to {actual}, expected {expected_intent}")
            print(
                f"  {'✓' if ok else '✗'} {question[:45]:<45} "
                f"{actual:<22} {elapsed:>5}ms  ${body['usage']['cost_usd']:.5f}"
            )
            print(f"      {body['answer'][:110]}")

        print("\n→ cost summary")
        cost = await client.get(f"{API}/admin/cost/summary?days=1", headers=headers)
        if cost.status_code == 200:
            print(f"  spend today: ${cost.json()['spend_today_usd']:.4f}")

    print("\n" + "=" * 70)
    if failures:
        print(f"FAILED ({len(failures)})")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("All smoke checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
