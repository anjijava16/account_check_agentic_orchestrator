"""FastAPI application entrypoint.

Startup order is deliberate. Dependencies that agents need must be live before
the graph is compiled, and the graph must be compiled before the first request
is served -- compiling per-request would add hundreds of milliseconds and
rebind the checkpointer every time.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import Response

from app.api.middleware.edge import (
    EdgeSecurityMiddleware,
    MetricsMiddleware,
    RateLimitMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)
from app.api.v1 import health
from app.api.v1.router import api_router
from app.core.config import settings
from app.core.exceptions import PlatformError
from app.core.logging import configure_logging, get_logger
from app.db.session import dispose_engine
from app.llm.cost_tracker import get_cost_tracker
from app.mcp.client.manager import get_mcp_manager
from app.memory.redis_client import close_redis
from app.observability.resource_monitor import ResourceMonitor
from app.observability.tracing import setup_tracing
from app.storage.s3 import get_object_store
from app.vector.opensearch_client import close_client, ensure_index

log = get_logger(__name__)
resource_monitor = ResourceMonitor()


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(settings.log_level, settings.log_json)
    setup_tracing(app)
    log.info("app.starting", environment=settings.environment)

    # 1. Object storage + search index must exist before ingestion or retrieval.
    await get_object_store().ensure_buckets()
    await ensure_index()

    # 2. Tool servers, so the graph binds a populated toolset.
    await get_mcp_manager().connect()

    # 3. Cost accounting, before any model call can happen.
    await get_cost_tracker().start()

    # 4. Checkpointer + graph. The checkpointer is held open for the process
    #    lifetime; closing it would break in-flight approval interrupts.
    from app.agents.graph import compile_graph
    from app.memory.checkpointer import checkpointer_lifespan

    async with checkpointer_lifespan() as checkpointer:
        compile_graph(checkpointer)
        await resource_monitor.start()
        log.info("app.ready")
        yield

    log.info("app.stopping")
    await resource_monitor.stop()
    await get_cost_tracker().stop()
    await get_mcp_manager().aclose()
    await close_client()
    await close_redis()
    await dispose_engine()
    log.info("app.stopped")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Agentic Banking Platform",
        version="1.0.0",
        description=(
            "Multi-agent banking assistant. LangGraph agent core, MCP tool servers, "
            "LiteLLM model gateway, hybrid OpenSearch + pgvector retrieval, "
            "Redis session store, human-in-the-loop approvals, full observability."
        ),
        lifespan=lifespan,
        docs_url=None if settings.is_prod else "/docs",
        redoc_url=None if settings.is_prod else "/redoc",
        openapi_url=None if settings.is_prod else "/openapi.json",
    )

    # Middleware runs bottom-up on the request path: the last added is outermost.
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(MetricsMiddleware)
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(EdgeSecurityMiddleware)
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["x-request-id", "x-response-time-ms"],
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_hosts)
    app.add_middleware(RequestContextMiddleware)

    app.include_router(health.router)
    app.include_router(api_router, prefix=settings.api_prefix)

    @app.exception_handler(PlatformError)
    async def platform_error_handler(request: Request, exc: PlatformError):
        request_id = getattr(request.state, "request_id", None)
        log.warning(
            "request.platform_error", code=exc.code, message=exc.message, path=request.url.path
        )
        return JSONResponse(status_code=exc.status_code, content=exc.to_payload(request_id))

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception):
        request_id = getattr(request.state, "request_id", None)
        log.exception("request.unhandled_error", path=request.url.path)
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "internal_error",
                    "message": "An unexpected error occurred",
                    "retryable": True,
                    "request_id": request_id,
                }
            },
        )

    @app.get(settings.metrics_path, include_in_schema=False)
    async def metrics() -> Response:
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, str]:
        return {
            "service": settings.app_name,
            "version": "1.0.0",
            "docs": "/docs" if not settings.is_prod else "disabled",
            "health": "/health",
        }

    return app


app = create_app()
