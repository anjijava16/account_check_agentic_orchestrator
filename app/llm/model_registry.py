"""Model catalogue.

Maps logical model names (what the code asks for) onto concrete deployments
(what actually serves the request), and carries the pricing used when the
provider doesn't return a cost. Mirrors the "Self Hosted LLM / Third-party LLM"
box in the architecture: sensitive traffic prefers self-hosted.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from app.core.config import settings


@dataclass(frozen=True, slots=True)
class ModelSpec:
    name: str
    litellm_model: str
    deployment: Literal["self_hosted", "third_party"]
    provider: str
    context_window: int
    input_cost_per_1k: float
    output_cost_per_1k: float
    supports_tools: bool = True
    supports_json_mode: bool = True
    data_classification_max: str = "internal"  # internal | confidential | restricted
    api_base: str | None = None
    rpm: int = 600
    tpm: int = 600_000

    def estimate_cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        return (prompt_tokens / 1000) * self.input_cost_per_1k + (
            completion_tokens / 1000
        ) * self.output_cost_per_1k


MODEL_REGISTRY: dict[str, ModelSpec] = {
    "self-hosted-llama-70b": ModelSpec(
        name="self-hosted-llama-70b",
        litellm_model="openai/llama-3.3-70b-instruct",
        deployment="self_hosted",
        provider="vllm",
        context_window=128_000,
        input_cost_per_1k=0.0004,
        output_cost_per_1k=0.0008,
        data_classification_max="restricted",
        api_base="http://vllm-70b.internal:8000/v1",
        rpm=1200,
    ),
    "self-hosted-llama-8b": ModelSpec(
        name="self-hosted-llama-8b",
        litellm_model="openai/llama-3.1-8b-instruct",
        deployment="self_hosted",
        provider="vllm",
        context_window=128_000,
        input_cost_per_1k=0.00006,
        output_cost_per_1k=0.00012,
        data_classification_max="restricted",
        api_base="http://vllm-8b.internal:8000/v1",
        rpm=6000,
    ),
    "self-hosted-bge-large": ModelSpec(
        name="self-hosted-bge-large",
        litellm_model="openai/bge-large-en-v1.5",
        deployment="self_hosted",
        provider="tei",
        context_window=512,
        input_cost_per_1k=0.00001,
        output_cost_per_1k=0.0,
        supports_tools=False,
        supports_json_mode=False,
        data_classification_max="restricted",
        api_base="http://tei-embeddings.internal:8080/v1",
        rpm=12000,
    ),
    "self-hosted-bge-reranker": ModelSpec(
        name="self-hosted-bge-reranker",
        litellm_model="openai/bge-reranker-v2-m3",
        deployment="self_hosted",
        provider="tei",
        context_window=512,
        input_cost_per_1k=0.00001,
        output_cost_per_1k=0.0,
        supports_tools=False,
        supports_json_mode=False,
        data_classification_max="restricted",
        api_base="http://tei-reranker.internal:8080/v1",
    ),
    "bedrock-claude": ModelSpec(
        name="bedrock-claude",
        litellm_model="bedrock/anthropic.claude-sonnet-4-20250514-v1:0",
        deployment="third_party",
        provider="bedrock",
        context_window=200_000,
        input_cost_per_1k=0.003,
        output_cost_per_1k=0.015,
        data_classification_max="confidential",
    ),
    "azure-gpt-4o": ModelSpec(
        name="azure-gpt-4o",
        litellm_model="azure/gpt-4o",
        deployment="third_party",
        provider="azure",
        context_window=128_000,
        input_cost_per_1k=0.0025,
        output_cost_per_1k=0.01,
        data_classification_max="confidential",
    ),
    "openai-gpt-4.1": ModelSpec(
        name="openai-gpt-4.1",
        litellm_model="openai/gpt-4.1",
        deployment="third_party",
        provider="openai",
        context_window=128_000,
        input_cost_per_1k=0.002,
        output_cost_per_1k=0.008,
        data_classification_max="confidential",
    ),
}


def build_router_model_list() -> list[dict[str, Any]]:
    """Translate the registry into litellm.Router's model_list format."""
    model_list: list[dict[str, Any]] = []
    for spec in MODEL_REGISTRY.values():
        params: dict[str, Any] = {"model": spec.litellm_model}
        if spec.api_base:
            params["api_base"] = spec.api_base
            params["api_key"] = settings.litellm_master_key
        model_list.append(
            {
                "model_name": spec.name,
                "litellm_params": params,
                "model_info": {
                    "deployment": spec.deployment,
                    "provider": spec.provider,
                    "max_input_tokens": spec.context_window,
                    "input_cost_per_token": spec.input_cost_per_1k / 1000,
                    "output_cost_per_token": spec.output_cost_per_1k / 1000,
                },
                "rpm": spec.rpm,
                "tpm": spec.tpm,
            }
        )
    return model_list


def select_model(classification: str = "internal", *, prefer_cheap: bool = False) -> str:
    """Pick a model that is allowed to see data of the given classification."""
    order = {"internal": 0, "confidential": 1, "restricted": 2}
    required = order.get(classification, 0)
    candidates = [
        spec
        for spec in MODEL_REGISTRY.values()
        if spec.supports_tools and order.get(spec.data_classification_max, 0) >= required
    ]
    if not candidates:
        return settings.primary_model
    # The configured primary model wins whenever it may see the data; only then
    # do we fall back to cheapest-first ordering.
    candidates.sort(key=lambda s: (s.name != settings.primary_model, s.input_cost_per_1k))
    return candidates[0].name
