"""Prometheus metric definitions.

Split along the two axes the Observability box calls out: application signals
(prompts, agent calls, tool calls) and resource signals (CPU/memory/disk).
"""
from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

LATENCY_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5, 8, 13, 21, 34, 60)

# --------------------------------------------------------------- HTTP layer
HTTP_REQUESTS = Counter(
    "http_requests_total", "HTTP requests", ["method", "route", "status"]
)
HTTP_LATENCY = Histogram(
    "http_request_duration_seconds", "HTTP latency", ["method", "route"], buckets=LATENCY_BUCKETS
)
EDGE_BLOCKS = Counter("edge_blocked_total", "Requests blocked at the edge", ["reason"])
RATE_LIMIT_HITS = Counter("rate_limit_hits_total", "Rate limit rejections", ["scope"])

# ----------------------------------------------------------------- LLM layer
LLM_LATENCY = Histogram(
    "llm_call_duration_seconds", "LLM latency", ["model", "deployment"], buckets=LATENCY_BUCKETS
)
LLM_TOKENS = Counter("llm_tokens_total", "Tokens consumed", ["model", "kind"])
LLM_COST = Counter("llm_cost_usd_total", "Model spend in USD", ["model", "deployment"])
LLM_ERRORS = Counter("llm_errors_total", "LLM call failures", ["model", "kind"])
PROMPT_SIZE = Histogram(
    "llm_prompt_tokens", "Prompt size distribution", ["agent"],
    buckets=(128, 256, 512, 1024, 2048, 4096, 8192, 16384, 32768),
)

# --------------------------------------------------------------- agent layer
AGENT_INVOCATIONS = Counter(
    "agent_invocations_total", "Agent node executions", ["agent", "status"]
)
AGENT_LATENCY = Histogram(
    "agent_duration_seconds", "Agent node latency", ["agent"], buckets=LATENCY_BUCKETS
)
GRAPH_TURNS = Counter("graph_turns_total", "Completed graph turns", ["outcome"])
ROUTING_DECISIONS = Counter("routing_decisions_total", "Coordinator routing", ["intent", "agent"])
HITL_INTERRUPTS = Counter("hitl_interrupts_total", "HITL approval gates", ["intent"])
GUARDRAIL_TRIPS = Counter("guardrail_trips_total", "Guardrail violations", ["kind"])

# ---------------------------------------------------------------- tool layer
TOOL_CALLS = Counter("mcp_tool_calls_total", "MCP tool calls", ["server", "tool", "status"])
TOOL_LATENCY = Histogram(
    "mcp_tool_duration_seconds", "MCP tool latency", ["server", "tool"], buckets=LATENCY_BUCKETS
)

# ------------------------------------------------------------ retrieval layer
RETRIEVAL_LATENCY = Histogram(
    "retrieval_duration_seconds", "Vector search latency", ["strategy"], buckets=LATENCY_BUCKETS
)
RETRIEVAL_RESULTS = Histogram(
    "retrieval_result_count", "Results returned", ["strategy"],
    buckets=(0, 1, 3, 5, 10, 20, 50, 100),
)
CACHE_EVENTS = Counter("semantic_cache_events_total", "Semantic cache", ["outcome"])

# ------------------------------------------------------------ ingestion layer
INGESTION_DOCS = Counter("ingestion_documents_total", "Documents ingested", ["status"])
INGESTION_STAGE_LATENCY = Histogram(
    "ingestion_stage_duration_seconds", "Ingestion stage latency", ["stage"],
    buckets=(0.1, 0.5, 1, 5, 10, 30, 60, 300, 900),
)
INGESTION_CHUNKS = Counter("ingestion_chunks_total", "Chunks produced", ["status"])
INGESTION_QUEUE_DEPTH = Gauge("ingestion_queue_depth", "Pending ingestion jobs")

# ------------------------------------------------------------ resource layer
PROCESS_CPU = Gauge("process_cpu_percent", "Process CPU utilisation")
PROCESS_MEMORY = Gauge("process_memory_bytes", "Process RSS")
DISK_USAGE = Gauge("disk_used_percent", "Disk utilisation", ["mount"])
DB_POOL_IN_USE = Gauge("db_pool_connections_in_use", "SQLAlchemy pool checkouts")
REDIS_POOL_IN_USE = Gauge("redis_pool_connections_in_use", "Redis pool checkouts")
