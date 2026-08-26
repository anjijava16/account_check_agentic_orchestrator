"""Structured logging with trace correlation.

Every log line carries request_id, session_id, user_id and the active OTel
trace/span ids so a single conversation can be reconstructed end to end.
"""
from __future__ import annotations

import logging
import sys
from contextvars import ContextVar
from typing import Any

import structlog
from opentelemetry import trace

request_id_ctx: ContextVar[str | None] = ContextVar("request_id", default=None)
session_id_ctx: ContextVar[str | None] = ContextVar("session_id", default=None)
user_id_ctx: ContextVar[str | None] = ContextVar("user_id", default=None)
agent_ctx: ContextVar[str | None] = ContextVar("agent", default=None)


def _add_context(_logger: Any, _name: str, event: dict[str, Any]) -> dict[str, Any]:
    for key, var in (
        ("request_id", request_id_ctx),
        ("session_id", session_id_ctx),
        ("user_id", user_id_ctx),
        ("agent", agent_ctx),
    ):
        value = var.get()
        if value:
            event[key] = value
    return event


def _add_trace(_logger: Any, _name: str, event: dict[str, Any]) -> dict[str, Any]:
    span = trace.get_current_span()
    ctx = span.get_span_context() if span else None
    if ctx and ctx.is_valid:
        event["trace_id"] = format(ctx.trace_id, "032x")
        event["span_id"] = format(ctx.span_id, "016x")
    return event


def configure_logging(level: str = "INFO", json_output: bool = True) -> None:
    renderer = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=True)
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _add_context,
            _add_trace,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level.upper())
    for noisy in ("uvicorn.access", "opensearch", "botocore", "urllib3", "LiteLLM"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
