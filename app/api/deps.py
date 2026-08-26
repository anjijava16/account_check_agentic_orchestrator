"""FastAPI dependencies."""
from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import settings
from app.core.constants import IDEMPOTENCY_HEADER
from app.core.exceptions import AuthenticationError, AuthorizationError
from app.core.logging import session_id_ctx, user_id_ctx
from app.memory.idempotency import IdempotencyStore
from app.memory.rate_limiter import RateLimiter
from app.security.auth import Principal, verify_token

bearer = HTTPBearer(auto_error=False)


async def current_principal(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> Principal:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        principal = await verify_token(credentials.credentials)
    except AuthenticationError as exc:
        raise HTTPException(status_code=401, detail=exc.message) from exc
    user_id_ctx.set(principal.subject)
    return principal


def require_scope(scope: str):
    async def _dependency(
        principal: Annotated[Principal, Depends(current_principal)],
    ) -> Principal:
        if scope not in principal.scopes and not principal.has_role("agent_operator"):
            raise AuthorizationError(f"Missing required scope: {scope}")
        return principal

    return _dependency


def require_role(*roles: str):
    async def _dependency(
        principal: Annotated[Principal, Depends(current_principal)],
    ) -> Principal:
        if not any(principal.has_role(r) for r in roles):
            raise AuthorizationError(f"Requires one of roles: {', '.join(roles)}")
        return principal

    return _dependency


async def per_user_rate_limit(
    principal: Annotated[Principal, Depends(current_principal)],
) -> None:
    limiter = RateLimiter(namespace="user")
    decision = await limiter.check(
        principal.subject, limit=settings.rate_limit_per_minute, window_seconds=60
    )
    if not decision.allowed:
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded",
            headers={"Retry-After": str(decision.retry_after_seconds)},
        )


async def idempotency_key(
    idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_HEADER)] = None,
) -> str | None:
    return idempotency_key


def get_idempotency_store() -> IdempotencyStore:
    return IdempotencyStore()


async def session_context(request: Request) -> uuid.UUID | None:
    raw = request.headers.get("x-session-id")
    if not raw:
        return None
    try:
        sid = uuid.UUID(raw)
    except ValueError:
        return None
    session_id_ctx.set(str(sid))
    return sid


PrincipalDep = Annotated[Principal, Depends(current_principal)]
