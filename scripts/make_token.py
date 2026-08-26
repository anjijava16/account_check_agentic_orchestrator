#!/usr/bin/env python3
"""Mint a local dev bearer token.

Only works when DEV_AUTH_BYPASS=true and ENVIRONMENT != prod.
"""
from __future__ import annotations

import argparse
import sys

from jose import jwt

from app.core.config import settings


def main() -> int:
    parser = argparse.ArgumentParser(description="Mint a local dev JWT")
    parser.add_argument("--sub", default="alex.rivera")
    parser.add_argument("--tenant", default="default")
    parser.add_argument("--customer", default="CUST-1001")
    parser.add_argument(
        "--roles", default="customer", help="Comma-separated, e.g. customer,agent_operator"
    )
    args = parser.parse_args()

    if settings.is_prod:
        print("Refusing to mint a dev token in prod.", file=sys.stderr)
        return 1

    claims = {
        "sub": args.sub,
        "tenant_id": args.tenant,
        "customer_ids": [args.customer],
        "roles": [r.strip() for r in args.roles.split(",") if r.strip()],
        "scopes": ["chat:write", "documents:read", "documents:write"],
        "channel": "web",
    }
    print(jwt.encode(claims, settings.dev_shared_secret, algorithm="HS256"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
