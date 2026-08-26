# Architecture

This document is the "why". The README is the "what and how".

## Request path, end to end

```
mobile / web client
   │  Bearer JWT, X-Session-Id, Idempotency-Key
   ▼
┌──────────────────────────────────────────────────────────────┐
│ Edge layer (middleware stack)                                │
│  RequestContext → BodyGuard → RateLimit → SecurityHeaders    │
│  then FastAPI dependency: authn against the IdP (JWKS)       │
└──────────────────────────────────────────────────────────────┘
   ▼
ChatRouter  ──────────────────────────────────► IngestionRouter
   │                                                  │
   │ hydrate history (Redis)                          │ S3 put → Postgres row
   ▼                                                  │ → arq enqueue → 202
LangGraph orchestrator                                ▼
   guard_input                                    worker pool
      ▼                                            parse (Docling)
   coordinator ── rules 60% / small LLM 40%        chunk (structure-aware)
      ├─ accounts_agent      → MCP accounts        embed (LiteLLM, cached)
      ├─ transaction_agent   → MCP transactions    ├─ Postgres (chunks+vectors)
      ├─ service_agent       → MCP service         └─ OpenSearch (bulk)
      ├─ knowledge_agent     → hybrid retrieval
      ├─ smalltalk / reject
      ▼
   guard_output  (groundedness, PII, advice)
      ▼
   finalize → answer + citations + cost + trace_id
```

## The five decisions that shaped everything else

### 1. The coordinator is a router, not a planner

A planner agent that decomposes arbitrary goals is the wrong shape for retail
banking. The domain is not open-ended -- it is roughly six intents, and
customers ask them in a hundred phrasings. A router gives bounded latency
(one hop, not N), a testable decision (route accuracy is a number), and a
security property that matters: an agent can only ever reach its own tools.

The cost is that genuinely multi-intent turns ("what's my balance and change my
address") get handled sequentially over two turns. That trade is worth it. The
state schema already supports fan-out via the `fanout` channel if that changes.

### 2. Tools are MCP servers, not Python functions

Three concrete wins:

- **Process isolation.** The service server -- the only one that can mutate
  customer data -- runs in its own pod with its own NetworkPolicy and its own
  credentials to the core banking system. A bug in transaction search cannot
  reach it.
- **Schema discovery.** Tool schemas come from `list_tools`, so adding a tool
  to a server makes it available to its agent without an API redeploy.
- **Reuse.** The same servers are consumable by other teams' agents, by an IDE,
  or by an internal ops assistant, with no coupling to this codebase.

The cost is a network hop per tool call (roughly 3-8ms in-cluster) and a
schema cache to avoid a `list_tools` round-trip per turn.

### 3. Postgres owns truth, OpenSearch owns retrieval

Chunk text and vectors are written to Postgres first and OpenSearch second. If
the bulk index fails, the document sits in `indexed_pending` and a retry replays
from Postgres -- no re-parse, no re-embed, no S3 read. Rebuilding the whole
index from scratch is a loop over `reindex_from_postgres`, and it costs zero
embedding dollars.

pgvector also gives exact KNN, which is how the eval suite measures what the
HNSW index is actually losing (recall@k against brute force).

### 4. Every model call goes through one gateway

LiteLLM is the only thing that talks to a model. That single choke point is
where routing, fallback, retry, cost accounting and the third-party data
boundary live. `allow_third_party=False` is a hard guarantee, enforced in one
function, that a payload never leaves the self-hosted fleet.

Without a gateway those five concerns get reimplemented per agent, and the
fifth one -- the data boundary -- gets forgotten in exactly one place.

### 5. Guardrails are deterministic first

Regex catches injection patterns in microseconds. The number-grounding check is
set arithmetic against the evidence. Only groundedness gets an LLM judge,
because there is no cheap deterministic test for it.

Running a judge model on every turn would roughly double both latency and cost
to catch a class of error that the deterministic checks already catch most of.

## Data boundaries

| Data | Where it lives | Retention | Leaves the bank? |
|---|---|---|---|
| Raw uploaded files | S3, SSE-KMS | Per records policy | No |
| Chunk text + vectors | Postgres, OpenSearch | Until document deleted | No |
| Conversation history | Redis (hot) | `SESSION_TTL_SECONDS` | No |
| Transcript (redacted) | Postgres | Audit retention | No |
| Prompts to self-hosted models | In-cluster only | Not stored | No |
| Prompts to third-party models | PII-tokenised before send | Vendor policy | Yes, tokenised |
| Traces | OTel collector → Jaeger | 7 days | No (attrs scrubbed) |

## Failure modes and what happens

| Failure | Behaviour |
|---|---|
| OpenSearch down | Chat degrades: knowledge route returns "can't find that", account/transaction routes unaffected. Ingestion queues and retries. |
| One retrieval leg fails | Hybrid search continues on the surviving leg, logs a warning. |
| Redis down | Rate limiting fails open (logged), sessions lose history, cost tracking degrades to Postgres-only. |
| Postgres down | Readiness fails, pod pulled from LB. Audit queue buffers up to 10k events then drops oldest with a counter. |
| Primary model unhealthy | LiteLLM cools it down for 30s and routes to the fallback deployment. |
| An MCP server down | That agent's tools error; the agent answers with what it has and says what it couldn't fetch. |
| Worker OOM | arq re-queues; job attempt increments; three attempts then `failed` with the error on the document row. |
