"""Central typed configuration.

Every knob the platform has is declared here so that nothing reads os.environ
directly at call sites. Settings are cached and immutable at runtime.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, PostgresDsn, RedisDsn, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", env_nested_delimiter="__"
    )

    # ---------------------------------------------------------------- runtime
    app_name: str = "agentic-banking-platform"
    environment: Literal["local", "dev", "uat", "prod"] = "local"
    debug: bool = False
    api_prefix: str = "/api/v1"
    workers: int = 4
    request_timeout_seconds: int = 60

    # ------------------------------------------------------------ edge layer
    cors_origins: list[str] = ["http://localhost:3000"]
    trusted_hosts: list[str] = ["*"]
    max_body_bytes: int = 25 * 1024 * 1024
    rate_limit_per_minute: int = 60
    rate_limit_burst: int = 20
    enable_waf_heuristics: bool = True

    # --------------------------------------------------------------- identity
    oidc_issuer: str = "https://identity.bank.internal"
    oidc_audience: str = "agentic-banking-platform"
    oidc_jwks_url: str = "https://identity.bank.internal/.well-known/jwks.json"
    jwt_algorithms: list[str] = ["RS256"]
    dev_auth_bypass: bool = True  # local only; hard-blocked when environment == prod
    dev_shared_secret: str = "local-dev-secret-change-me"

    # -------------------------------------------------------------- postgres
    postgres_dsn: PostgresDsn = "postgresql+asyncpg://banking:banking@localhost:5432/banking"  # type: ignore[assignment]
    postgres_sync_dsn: str = "postgresql+psycopg://banking:banking@localhost:5432/banking"
    db_pool_size: int = 20
    db_max_overflow: int = 10
    db_pool_recycle_seconds: int = 1800
    db_echo: bool = False

    # ----------------------------------------------------------------- redis
    redis_dsn: RedisDsn = "redis://localhost:6379/0"  # type: ignore[assignment]
    redis_max_connections: int = 100
    session_ttl_seconds: int = 60 * 60 * 12
    shared_state_ttl_seconds: int = 60 * 30
    idempotency_ttl_seconds: int = 60 * 60 * 24
    semantic_cache_ttl_seconds: int = 60 * 60

    # ------------------------------------------------------------ opensearch
    opensearch_hosts: list[str] = ["http://localhost:9200"]
    opensearch_user: str | None = "admin"
    opensearch_password: str | None = "admin"
    opensearch_verify_certs: bool = False
    opensearch_index_alias: str = "kb-chunks"
    opensearch_index_prefix: str = "kb-chunks-v"
    opensearch_shards: int = 3
    opensearch_replicas: int = 1
    knn_ef_search: int = 128
    knn_m: int = 16
    knn_ef_construction: int = 256
    hybrid_bm25_weight: float = 0.4
    hybrid_knn_weight: float = 0.6
    rrf_k: int = 60

    # ------------------------------------------------------------------- s3
    s3_endpoint_url: str | None = "http://localhost:9000"
    s3_region: str = "us-east-1"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    s3_raw_bucket: str = "banking-raw-documents"
    s3_processed_bucket: str = "banking-processed-documents"
    s3_presign_ttl_seconds: int = 900
    s3_sse_kms_key_id: str | None = None
    # MinIO without a configured KMS rejects any SSE header; disable for local dev.
    s3_sse_enabled: bool = True

    # -------------------------------------------------------------- litellm
    litellm_base_url: str = "http://localhost:4000"
    litellm_master_key: str = "sk-local-master-key"
    primary_model: str = "self-hosted-llama-70b"
    fallback_models: list[str] = ["bedrock-claude", "azure-gpt-4o"]
    router_model: str = "self-hosted-llama-8b"
    embedding_model: str = "self-hosted-bge-large"
    embedding_dimensions: int = 1024
    llm_timeout_seconds: int = 45
    llm_max_retries: int = 2
    llm_temperature: float = 0.1
    llm_max_tokens: int = 1536

    # --------------------------------------------------------------- agents
    agent_recursion_limit: int = 24
    agent_max_tool_calls: int = 8
    enable_human_in_the_loop: bool = True
    hitl_risk_threshold: float = 0.7
    checkpointer: Literal["postgres", "redis", "memory"] = "postgres"

    # ------------------------------------------------------------ mcp servers
    mcp_accounts_url: str = "http://localhost:8081/mcp"
    mcp_transactions_url: str = "http://localhost:8082/mcp"
    mcp_service_url: str = "http://localhost:8083/mcp"
    mcp_transport: Literal["streamable_http", "sse", "stdio"] = "streamable_http"
    mcp_call_timeout_seconds: int = 20

    # -------------------------------------------------------------- ingestion
    chunk_size_tokens: int = 512
    chunk_overlap_tokens: int = 64
    max_chunks_per_document: int = 5000
    embedding_batch_size: int = 32
    ingestion_queue: str = "ingestion"
    ingestion_max_retries: int = 5
    parser_backend: Literal["auto", "docling", "native"] = "auto"

    # ------------------------------------------------------------- security
    pii_redaction_enabled: bool = True
    pii_redaction_backend: Literal["regex", "presidio"] = "regex"
    redact_outbound_to_third_party_llm: bool = True
    authz_policy_file: str = "app/security/policies.yaml"

    # --------------------------------------------------------- observability
    otel_enabled: bool = True
    otel_endpoint: str = "http://localhost:4317"
    otel_service_name: str = "agentic-banking-platform"
    log_level: str = "INFO"
    log_json: bool = True
    metrics_path: str = "/metrics"
    cost_tracking_enabled: bool = True
    cost_alert_daily_usd: float = 500.0

    # --------------------------------------------------------------- evals
    eval_enabled: bool = True
    eval_sample_rate: float = 0.10
    eval_dataset_path: str = "app/evaluation/datasets/golden.jsonl"

    @field_validator("dev_auth_bypass")
    @classmethod
    def _no_bypass_in_prod(cls, v: bool, info) -> bool:
        if v and info.data.get("environment") == "prod":
            raise ValueError("dev_auth_bypass cannot be enabled in prod")
        return v

    @property
    def is_prod(self) -> bool:
        return self.environment == "prod"


@lru_cache(maxsize=1)
def get_settings() -> AppSettings:
    return AppSettings()


settings = get_settings()
