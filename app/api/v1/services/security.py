"""Security / token utilities.

`GET /token` introspects the caller's own verified principal (any authenticated
user). `POST /token` mints a local dev JWT and is hard-disabled outside dev
(`dev_auth_bypass` + non-prod), mirroring scripts/make_token.py.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from jose import jwt
from pydantic import BaseModel, Field

from app.api.deps import PrincipalDep, current_principal
from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)
router = APIRouter(
    prefix="/services/security",
    tags=["services: security"],
    dependencies=[Depends(current_principal)],
)


class TokenMintRequest(BaseModel):
    sub: str = Field(default="alex.rivera", max_length=128)
    tenant_id: str = Field(default="default", max_length=64)
    customer_ids: list[str] = Field(default_factory=lambda: ["CUST-1001"])
    roles: list[str] = Field(default_factory=lambda: ["customer"])
    scopes: list[str] = Field(
        default_factory=lambda: ["chat:write", "documents:read", "documents:write"]
    )
    channel: str = Field(default="web", max_length=32)


@router.get("/token", summary="Introspect the caller's token")
async def token_info(principal: PrincipalDep) -> dict[str, Any]:
    return {
        "subject": principal.subject,
        "tenant_id": principal.tenant_id,
        "username": principal.username,
        "email": principal.email,
        "roles": principal.roles,
        "scopes": principal.scopes,
        "customer_ids": principal.customer_ids,
        "channel": principal.channel,
        "claims": principal.claims,
    }


@router.post("/token", summary="Mint a dev token (dev only)")
async def mint_token(body: TokenMintRequest) -> dict[str, Any]:
    if settings.is_prod or not settings.dev_auth_bypass:
        raise HTTPException(
            status_code=403, detail="Dev token minting is disabled in this environment"
        )
    claims = {
        "sub": body.sub,
        "tenant_id": body.tenant_id,
        "customer_ids": body.customer_ids,
        "roles": body.roles,
        "scopes": body.scopes,
        "channel": body.channel,
    }
    token = jwt.encode(claims, settings.dev_shared_secret, algorithm="HS256")
    return {"access_token": token, "token_type": "bearer", "claims": claims}
