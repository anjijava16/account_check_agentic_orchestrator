"""Authentication against the bank's identity provider (OIDC/JWT).

JWKS is fetched once and cached; keys rotate without a redeploy. In local mode
a shared-secret HS256 token is accepted so the stack runs without an IdP.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from jose import jwt
from jose.exceptions import JWTError

from app.core.config import settings
from app.core.exceptions import AuthenticationError
from app.core.logging import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class Principal:
    subject: str
    tenant_id: str
    username: str | None = None
    email: str | None = None
    roles: list[str] = field(default_factory=list)
    scopes: list[str] = field(default_factory=list)
    customer_ids: list[str] = field(default_factory=list)
    channel: str = "web"
    claims: dict[str, Any] = field(default_factory=dict)

    def has_role(self, role: str) -> bool:
        return role in self.roles

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes

    def owns(self, customer_id: str) -> bool:
        return customer_id in self.customer_ids


class JWKSCache:
    def __init__(self, url: str, ttl: int = 3600) -> None:
        self.url = url
        self.ttl = ttl
        self._keys: dict[str, Any] | None = None
        self._fetched_at = 0.0

    async def keys(self) -> dict[str, Any]:
        if self._keys is None or time.time() - self._fetched_at > self.ttl:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(self.url)
                response.raise_for_status()
                self._keys = response.json()
                self._fetched_at = time.time()
                log.info("auth.jwks_refreshed", keys=len(self._keys.get("keys", [])))
        return self._keys


_jwks = JWKSCache(settings.oidc_jwks_url)


async def verify_token(token: str) -> Principal:
    if not token:
        raise AuthenticationError("Missing bearer token")

    if settings.dev_auth_bypass and not settings.is_prod:
        return _decode_dev_token(token)

    try:
        jwks = await _jwks.keys()
        claims = jwt.decode(
            token,
            jwks,
            algorithms=settings.jwt_algorithms,
            audience=settings.oidc_audience,
            issuer=settings.oidc_issuer,
            options={"verify_at_hash": False},
        )
    except JWTError as exc:
        log.warning("auth.token_rejected", error=str(exc))
        raise AuthenticationError("Invalid or expired token") from exc
    except httpx.HTTPError as exc:
        raise AuthenticationError("Identity provider unreachable") from exc

    return _to_principal(claims)


def _decode_dev_token(token: str) -> Principal:
    try:
        claims = jwt.decode(
            token,
            settings.dev_shared_secret,
            algorithms=["HS256"],
            options={"verify_aud": False, "verify_iss": False},
        )
    except JWTError:
        # Unsigned convenience token for local curl-driven testing.
        claims = {
            "sub": token[:64] or "local-user",
            "tenant_id": "default",
            "roles": ["customer"],
            "scopes": ["chat:write", "documents:read", "documents:write"],
            "customer_ids": ["CUST-1001"],
        }
    return _to_principal(claims)


def _to_principal(claims: dict[str, Any]) -> Principal:
    roles = claims.get("roles") or claims.get("realm_access", {}).get("roles", []) or []
    scope_claim = claims.get("scope") or claims.get("scp") or ""
    scopes = scope_claim.split() if isinstance(scope_claim, str) else list(scope_claim)
    scopes = scopes or claims.get("scopes", [])
    return Principal(
        subject=claims.get("sub", "unknown"),
        tenant_id=claims.get("tenant_id") or claims.get("org") or "default",
        username=claims.get("preferred_username") or claims.get("name"),
        email=claims.get("email"),
        roles=list(roles),
        scopes=list(scopes),
        customer_ids=list(claims.get("customer_ids", [])),
        channel=claims.get("channel", "web"),
        claims=claims,
    )
