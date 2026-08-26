"""Domain exceptions mapped to stable API error codes."""
from __future__ import annotations

from typing import Any


class PlatformError(Exception):
    """Base error. `code` is a stable machine-readable identifier."""

    status_code: int = 500
    code: str = "internal_error"
    retryable: bool = False

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_payload(self, request_id: str | None = None) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "retryable": self.retryable,
                "details": self.details,
                "request_id": request_id,
            }
        }


class ValidationError(PlatformError):
    status_code, code = 422, "validation_error"


class AuthenticationError(PlatformError):
    status_code, code = 401, "unauthenticated"


class AuthorizationError(PlatformError):
    status_code, code = 403, "forbidden"


class NotFoundError(PlatformError):
    status_code, code = 404, "not_found"


class ConflictError(PlatformError):
    status_code, code = 409, "conflict"


class RateLimitError(PlatformError):
    status_code, code, retryable = 429, "rate_limited", True


class UpstreamError(PlatformError):
    status_code, code, retryable = 502, "upstream_error", True


class LLMError(UpstreamError):
    code = "llm_error"


class ToolExecutionError(UpstreamError):
    code = "tool_execution_error"


class VectorStoreError(UpstreamError):
    code = "vector_store_error"


class StorageError(UpstreamError):
    code = "object_storage_error"


class BudgetExceededError(PlatformError):
    status_code, code = 402, "budget_exceeded"


class GuardrailViolation(PlatformError):
    status_code, code = 400, "guardrail_violation"
