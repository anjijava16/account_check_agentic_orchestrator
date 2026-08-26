"""Edge Layer middleware.

The WAF/DDoS/API-Gateway box from the architecture, implemented at the
application boundary. In a real deployment CloudFront/ALB/Kong sit in front of
this and do the heavy lifting; these checks are the defence-in-depth layer that
still applies when something reaches the pod directly.

Order matters -- cheapest rejection first:
  request id -> body size -> WAF heuristics -> rate limit -> timing
"""
from __future__ import annotations

import time
import uuid

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from app.core.config import settings
from app.core.constants import TRACE_HEADER
from app.core.logging import get_logger, request_id_ctx
from app.memory.rate_limiter import RateLimiter
from app.observability.metrics import EDGE_BLOCKS, HTTP_LATENCY, HTTP_REQUESTS, RATE_LIMIT_HITS

log = get_logger(__name__)

SUSPICIOUS_PATHS = (
    "/.env", "/.git", "/wp-admin", "/phpmyadmin", "/actuator", "/config.json",
    "/admin/config", "/.aws/credentials",
)
SUSPICIOUS_UA = ("sqlmap", "nikto", "nmap", "masscan", "havij", "acunetix")
EXEMPT_PATHS = ("/health", "/ready", "/live", "/metrics")


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assign a request id and expose it on the response + logs."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get(TRACE_HEADER) or str(uuid.uuid4())
        request_id_ctx.set(request_id)
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers[TRACE_HEADER] = request_id
        return response


class EdgeSecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path
        if path in EXEMPT_PATHS:
            return await call_next(request)

        if settings.enable_waf_heuristics:
            blocked = self._waf_check(request)
            if blocked:
                EDGE_BLOCKS.labels(reason=blocked).inc()
                log.warning("edge.blocked", reason=blocked, path=path, client=self._client(request))
                return JSONResponse(
                    status_code=403,
                    content={"error": {"code": "forbidden", "message": "Request rejected"}},
                )

        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > settings.max_body_bytes:
            EDGE_BLOCKS.labels(reason="body_too_large").inc()
            return JSONResponse(
                status_code=413,
                content={"error": {"code": "payload_too_large", "message": "Body too large"}},
            )

        return await call_next(request)

    def _waf_check(self, request: Request) -> str | None:
        path = request.url.path.lower()
        if any(bad in path for bad in SUSPICIOUS_PATHS):
            return "path_scanning"
        ua = request.headers.get("user-agent", "").lower()
        if any(tool in ua for tool in SUSPICIOUS_UA):
            return "scanner_user_agent"
        if "://" in request.url.query and "redirect" in request.url.query.lower():
            return "open_redirect_attempt"
        if len(request.url.query) > 4096:
            return "query_too_long"
        return None

    @staticmethod
    def _client(request: Request) -> str:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "unknown"


class RateLimitMiddleware(BaseHTTPMiddleware):
    """IP-level limiting. Per-user limiting happens in the route dependency,
    which is stricter but needs an authenticated principal."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path in EXEMPT_PATHS:
            return await call_next(request)

        client = EdgeSecurityMiddleware._client(request)
        try:
            decision = await RateLimiter(namespace="ip").check(
                client, limit=settings.rate_limit_per_minute * 3, window_seconds=60
            )
        except Exception:  # noqa: BLE001
            # Never fail closed on a Redis blip -- the per-user limit still applies.
            return await call_next(request)

        if not decision.allowed:
            RATE_LIMIT_HITS.labels(scope="ip").inc()
            return JSONResponse(
                status_code=429,
                content={
                    "error": {
                        "code": "rate_limited",
                        "message": "Too many requests",
                        "retryable": True,
                    }
                },
                headers={"Retry-After": str(decision.retry_after_seconds)},
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(decision.limit)
        response.headers["X-RateLimit-Remaining"] = str(max(0, decision.limit - decision.current))
        return response


class MetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        started = time.perf_counter()
        route = request.scope.get("route")
        template = getattr(route, "path", request.url.path)
        try:
            response = await call_next(request)
            status_code = response.status_code
        except Exception:
            status_code = 500
            raise
        finally:
            elapsed = time.perf_counter() - started
            HTTP_LATENCY.labels(method=request.method, route=template).observe(elapsed)
            HTTP_REQUESTS.labels(
                method=request.method, route=template, status=str(status_code)
            ).inc()
        response.headers["X-Response-Time-Ms"] = str(int(elapsed * 1000))
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        response.headers.update(
            {
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "no-referrer",
                "Strict-Transport-Security": "max-age=63072000; includeSubDomains",
                "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
                "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
            }
        )
        return response
