"""LiteLLM gateway.

Single choke point for every model call in the platform. Responsibilities:
  * routing across self-hosted and third-party deployments with fallbacks
  * outbound PII redaction before anything leaves the bank's network
  * per-call cost + token accounting (emitted to the Cost Tracker)
  * retries with jittered backoff, circuit breaking on repeated 5xx
  * OTel spans with model, deployment, tokens and latency attributes
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Literal

import litellm
from litellm import Router
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from app.core.config import settings
from app.core.exceptions import LLMError
from app.core.logging import get_logger
from app.llm.model_registry import MODEL_REGISTRY, ModelSpec, build_router_model_list
from app.observability.metrics import LLM_LATENCY, LLM_TOKENS, LLM_ERRORS
from app.security.pii import redact_messages

log = get_logger(__name__)

litellm.drop_params = True
litellm.set_verbose = False
litellm.suppress_debug_info = True


@dataclass(slots=True)
class LLMUsage:
    model: str
    deployment: Literal["self_hosted", "third_party"]
    provider: str
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    total_tokens: int
    cost_usd: float
    latency_ms: int
    success: bool = True
    fallback_used: bool = False


@dataclass(slots=True)
class LLMResponse:
    content: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    finish_reason: str | None = None
    usage: LLMUsage | None = None
    raw: Any = None


class LLMGateway:
    """Thin, opinionated wrapper over litellm.Router."""

    def __init__(self) -> None:
        self._router: Router | None = None
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0

    # ------------------------------------------------------------ lifecycle
    def router(self) -> Router:
        if self._router is None:
            self._router = Router(
                model_list=build_router_model_list(),
                routing_strategy="latency-based-routing",
                num_retries=settings.llm_max_retries,
                timeout=settings.llm_timeout_seconds,
                allowed_fails=3,
                cooldown_time=30,
                fallbacks=(
                    [{settings.primary_model: settings.fallback_models}]
                    if settings.fallback_models
                    else []
                ),
                enable_pre_call_checks=True,
                cache_responses=False,
            )
            log.info(
                "llm.router_initialised",
                primary=settings.primary_model,
                fallbacks=settings.fallback_models,
            )
        return self._router

    async def aclose(self) -> None:
        self._router = None

    # -------------------------------------------------------------- helpers
    def _spec(self, model: str) -> ModelSpec:
        return MODEL_REGISTRY.get(model, MODEL_REGISTRY[settings.primary_model])

    def _circuit_check(self) -> None:
        if time.time() < self._circuit_open_until:
            raise LLMError(
                "Model gateway circuit is open; upstream repeatedly failing",
                details={"retry_after_seconds": int(self._circuit_open_until - time.time())},
            )

    def _record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= 5:
            self._circuit_open_until = time.time() + 30
            self._consecutive_failures = 0
            log.error("llm.circuit_opened", seconds=30)

    # ----------------------------------------------------------- completion
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(initial=0.5, max=6),
        retry=retry_if_exception_type(LLMError),
        reraise=True,
    )
    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        stream: bool = False,
    ) -> LLMResponse:
        self._circuit_check()
        model = model or settings.primary_model
        spec = self._spec(model)

        # Redact before the payload can cross a network boundary we don't own.
        outbound = messages
        if settings.redact_outbound_to_third_party_llm and spec.deployment == "third_party":
            outbound = redact_messages(messages)

        params: dict[str, Any] = {
            "model": model,
            "messages": outbound,
            "temperature": settings.llm_temperature if temperature is None else temperature,
            "max_tokens": max_tokens or settings.llm_max_tokens,
            "timeout": settings.llm_timeout_seconds,
            "metadata": {"platform": settings.app_name, **(metadata or {})},
        }
        if tools:
            params["tools"] = tools
            params["tool_choice"] = tool_choice or "auto"
        if response_format:
            params["response_format"] = response_format

        started = time.perf_counter()
        try:
            raw = await self.router().acompletion(**params)
        except Exception as exc:  # noqa: BLE001
            self._record_failure()
            LLM_ERRORS.labels(model=model, kind=type(exc).__name__).inc()
            log.error("llm.call_failed", model=model, error=str(exc))
            raise LLMError(f"Model call failed for {model}: {exc}") from exc

        self._consecutive_failures = 0
        latency_ms = int((time.perf_counter() - started) * 1000)
        return self._to_response(raw, model, spec, latency_ms)

    def _to_response(
        self, raw: Any, model: str, spec: ModelSpec, latency_ms: int
    ) -> LLMResponse:
        choice = raw.choices[0]
        message = choice.message
        usage_obj = getattr(raw, "usage", None)
        prompt_tokens = getattr(usage_obj, "prompt_tokens", 0) or 0
        completion_tokens = getattr(usage_obj, "completion_tokens", 0) or 0
        cached = 0
        details = getattr(usage_obj, "prompt_tokens_details", None)
        if details is not None:
            cached = getattr(details, "cached_tokens", 0) or 0

        try:
            cost = float(litellm.completion_cost(completion_response=raw))
        except Exception:  # noqa: BLE001
            cost = spec.estimate_cost(prompt_tokens, completion_tokens)

        actual_model = getattr(raw, "model", model) or model
        usage = LLMUsage(
            model=actual_model,
            deployment=spec.deployment,
            provider=spec.provider,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cached_tokens=cached,
            total_tokens=prompt_tokens + completion_tokens,
            cost_usd=round(cost, 6),
            latency_ms=latency_ms,
            fallback_used=actual_model != model,
        )
        LLM_LATENCY.labels(model=actual_model, deployment=spec.deployment).observe(
            latency_ms / 1000
        )
        LLM_TOKENS.labels(model=actual_model, kind="prompt").inc(prompt_tokens)
        LLM_TOKENS.labels(model=actual_model, kind="completion").inc(completion_tokens)

        tool_calls = []
        for call in getattr(message, "tool_calls", None) or []:
            tool_calls.append(
                {
                    "id": call.id,
                    "name": call.function.name,
                    "arguments": call.function.arguments,
                }
            )
        return LLMResponse(
            content=message.content or "",
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason,
            usage=usage,
            raw=raw,
        )

    # ----------------------------------------------------------- embeddings
    async def embed(
        self, texts: list[str], *, model: str | None = None
    ) -> tuple[list[list[float]], LLMUsage]:
        model = model or settings.embedding_model
        spec = self._spec(model)
        started = time.perf_counter()
        try:
            raw = await litellm.aembedding(
                model=model,
                input=texts,
                api_base=settings.litellm_base_url,
                api_key=settings.litellm_master_key,
                timeout=settings.llm_timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            LLM_ERRORS.labels(model=model, kind="embedding").inc()
            raise LLMError(f"Embedding call failed: {exc}") from exc

        latency_ms = int((time.perf_counter() - started) * 1000)
        vectors = [item["embedding"] for item in raw.data]
        prompt_tokens = getattr(getattr(raw, "usage", None), "prompt_tokens", 0) or 0
        usage = LLMUsage(
            model=model,
            deployment=spec.deployment,
            provider=spec.provider,
            prompt_tokens=prompt_tokens,
            completion_tokens=0,
            cached_tokens=0,
            total_tokens=prompt_tokens,
            cost_usd=round(spec.estimate_cost(prompt_tokens, 0), 8),
            latency_ms=latency_ms,
        )
        return vectors, usage

    async def embed_batched(
        self, texts: list[str], *, batch_size: int | None = None, concurrency: int = 4
    ) -> tuple[list[list[float]], list[LLMUsage]]:
        batch_size = batch_size or settings.embedding_batch_size
        batches = [texts[i : i + batch_size] for i in range(0, len(texts), batch_size)]
        sem = asyncio.Semaphore(concurrency)

        async def run(batch: list[str]):
            async with sem:
                return await self.embed(batch)

        results = await asyncio.gather(*(run(b) for b in batches))
        vectors: list[list[float]] = []
        usages: list[LLMUsage] = []
        for vecs, usage in results:
            vectors.extend(vecs)
            usages.append(usage)
        return vectors, usages


_gateway: LLMGateway | None = None


def get_gateway() -> LLMGateway:
    global _gateway
    if _gateway is None:
        _gateway = LLMGateway()
    return _gateway
