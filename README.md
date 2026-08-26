# Agentic Banking Platform

A multi-agent conversational banking system, built the way you'd actually have
to build it if it were going to carry real customer traffic.

A coordinator routes each message to one of three specialist agents. Every
specialist reaches its capabilities through an MCP server rather than through
in-process function calls. Anything that mutates a customer record stops at a
human approval gate. Documents uploaded to the knowledge base go through a
separate ingestion pipeline that lands vectors in OpenSearch and every piece of
metadata in Postgres, and retrieval is hybrid BM25 + kNN with reranking and
citations.

This is the reference implementation of the architecture in the design diagram
— edge layer, authentication, coordinator, specialist agents, MCP servers,
session store, cost tracker, PII redaction, observability, evaluation suite —
wired together so it runs end to end.

```
   User Interface (chat / mobile / branch console)
                      │
   ┌──────────────────▼──────────────────────────────────────────┐
   │  EDGE LAYER                                                 │
   │  body-size guard · WAF heuristics · Redis token bucket ·    │
   │  security headers · request context + correlation IDs       │
   └──────────────────┬──────────────────────────────────────────┘
                      │
   ┌──────────────────▼──────────────────────────────────────────┐
   │  AUTHENTICATION — JWT verified against the bank's IdP        │
   │  (JWKS, RS256). Principal carries subject, tenant, roles,   │
   │  scopes, and the customer IDs this caller may act on.       │
   └──────────────────┬──────────────────────────────────────────┘
                      │
   ┌──────────────────▼──────────────────────────────────────────┐
   │  FastAPI                                                     │
   │  ChatRouter · IngestionRouter · AdminRouter · EvalRouter     │
   └────────┬───────────────────────────────┬────────────────────┘
            │                               │
   ┌────────▼─────────────────┐   ┌─────────▼──────────────────────┐
   │  LangGraph orchestrator  │   │  Ingestion pipeline (arq)      │
   │                          │   │                                │
   │  guardrail               │   │  S3 → parse → chunk → embed →  │
   │      ▼                   │   │  Postgres (truth) +            │
   │  coordinator ────────────┼─┐ │  OpenSearch (retrieval)        │
   │      ├── accounts   ─────┼─┼─┼──► MCP: accounts    :8081      │
   │      ├── transactions ───┼─┼─┼──► MCP: transactions :8082     │
   │      └── service ────────┼─┼─┼──► MCP: service      :8083     │
   │           ▼              │ │ └────────────────────────────────┘
   │       approval (HITL)    │ │
   │           ▼              │ └──► hybrid retrieval
   │       synthesis          │      (BM25 + kNN + RRF + rerank)
   └──────────┬───────────────┘
              │
   ┌──────────▼──────────────────────────────────────────────────┐
   │  CROSS-CUTTING                                               │
   │  Session store (Redis) · Checkpointer (Postgres) ·           │
   │  LiteLLM gateway (self-hosted first, third-party fallback) · │
   │  Cost tracker · PII redaction · OTel traces + Prometheus ·   │
   │  Evaluation suite                                            │
   └─────────────────────────────────────────────────────────────┘
```

---

## Contents

1. [Quick start](#quick-start)
2. [Why it's shaped this way](#why-its-shaped-this-way)
3. [The agent graph](#the-agent-graph)
4. [Why MCP instead of function calling](#why-mcp-instead-of-function-calling)
5. [The ingestion pipeline](#the-ingestion-pipeline)
6. [Retrieval](#retrieval)
7. [The model gateway](#the-model-gateway)
8. [Security](#security)
9. [Human in the loop](#human-in-the-loop)
10. [Observability and cost](#observability-and-cost)
11. [Evaluation](#evaluation)
12. [Data model](#data-model)
13. [Project layout](#project-layout)
14. [Operating it](#operating-it)
15. [What's deliberately unfinished](#whats-deliberately-unfinished)

---

## Quick start

```bash
cp .env.example .env
make up            # 14 containers: API, 2 workers, 3 MCP servers, Postgres+pgvector,
                   # Redis, OpenSearch, MinIO, LiteLLM, OTel, Jaeger, Prometheus, Grafana
make seed          # load four sample policy documents through the real ingestion path
make smoke         # end-to-end: upload, wait for indexing, run one turn per intent
```

Then:

| What | Where |
|---|---|
| API docs | http://localhost:8000/docs |
| Health report | http://localhost:8000/health |
| Traces | http://localhost:16686 |
| Dashboards | http://localhost:3000 (admin/admin) |
| Object store | http://localhost:9001 (minioadmin/minioadmin) |
| Search cluster | http://localhost:5601 |

A dev token:

```bash
make token
# or with a role:
python scripts/make_token.py --sub ops.user --roles customer,agent_operator
```

One turn:

```bash
curl -s localhost:8000/api/v1/chat \
  -H "Authorization: Bearer $(make -s token)" \
  -H 'Content-Type: application/json' \
  -d '{"message":"How long does an international transfer take?"}' | jq
```

Running without Docker: `make install-all && make migrate && make index`, then
`make dev`, `make worker`, and `make mcp` in three shells.

---

## Why it's shaped this way

Five decisions drove nearly everything else. Each one traded something away.

**The coordinator classifies; it does not plan.** A planner that decomposes an
arbitrary request into arbitrary tool sequences is the standard demo, and it is
very hard to certify in a regulated environment — you can't enumerate what it
will do. Here the coordinator emits one intent from a closed set of nine, and
the intent deterministically selects an agent. Every possible path through the
system can be drawn on one page and handed to a risk reviewer. The cost is that
a genuinely multi-intent message ("what's my balance and also change my
address") gets handled one intent at a time. That's a real limitation and it's
the right trade for this domain.

**Tools live behind MCP servers, not in the agent process.** More on this
below, but the short version: the credentials for core banking live in the MCP
tier and nowhere else, so a compromised agent pod can't reach the systems of
record directly. A NetworkPolicy enforces it.

**Postgres is the source of truth; OpenSearch is a derived index.** Every
chunk, its text, its metadata and its embedding are written to Postgres with
pgvector. OpenSearch holds a copy for retrieval. That means a mapping change,
an analyzer change, or a corrupted index costs a re-index from Postgres —
storage time only — instead of re-embedding a corpus, which costs real money
and hours. The duplication is deliberate.

**All model traffic exits through one gateway.** Nothing in the application
calls a provider SDK. Everything goes through LiteLLM under a logical model name
(`self-hosted-llama-70b`), which means the routing policy, the fallback chain,
the spend ledger and the redaction boundary all have exactly one place to live.

**Deterministic checks run before probabilistic ones.** Rate limiting, body
guards, JWT verification, tool authorization, ownership checks and regex
guardrails all run before a model is consulted, and the model's output never
widens what a caller is allowed to do. The model chooses *which* permitted tool
to call; it never decides *whether* the caller is permitted.

---

## The agent graph

```
START → guardrail → coordinator ─┬→ accounts ──────┐
                                 ├→ transactions ──┼→ synthesis → END
                                 ├→ service ───┬───┘
                                 │             └→ approval → synthesis
                                 └→ (rejected / small talk) → synthesis
```

Seven nodes, compiled once at startup in `app/agents/graph.py` and held for the
process lifetime. Compiling per request would add hundreds of milliseconds and
rebind the checkpointer on every turn.

**guardrail** — input checks before anything expensive happens. Tool-coercion
patterns ("transfer all funds to…") and cross-customer probes ("show balances
for all customers") are hard blocks that short-circuit straight to synthesis.
Instruction-override patterns are flagged and carried forward as a risk signal
rather than blocked, because "ignore that, I meant my savings account" is a
perfectly normal thing for a customer to say.

**coordinator** — routing, in two stages. A keyword fast path handles the
unambiguous cases without a model call at all; on the seeded traffic that's
roughly 40% of turns, at zero cost and about a millisecond. Everything else
goes to the small 8B model with a constrained JSON output. If the model's
confidence lands below the floor, the turn degrades to a knowledge lookup
rather than guessing at an account action — the failure mode of "I searched the
policy documents for you" is much better than the failure mode of "I ordered
you a cheque book".

**accounts / transactions / service** — the three specialists. They share one
ReAct-style loop with a hard tool-call ceiling (`AGENT_MAX_TOOL_CALLS`, default
8). Each is bound only to the tools of its own MCP server, so the transaction
agent cannot see the address-change tool even if a prompt injection asks it to.
Multiple tool calls in one step run concurrently.

**approval** — the service agent stages mutations instead of performing them.
When the intent is in `HIGH_RISK_INTENTS` the node calls LangGraph's
`interrupt()`, which persists the entire graph state to the Postgres
checkpointer and returns. The API responds `status: "pending_approval"`. The
conversation survives a pod restart, a deploy, or an overnight wait.

**synthesis** — turns tool results into an answer, attaches citations, runs
output guardrails (system-prompt leakage, PII that shouldn't be echoed),
persists the turn, and records cost.

State (`app/agents/state.py`) uses append-only reducers for messages, tool
traces and citations, so parallel branches merge instead of clobbering each
other.

---

## Why MCP instead of function calling

Binding Python functions to the model as tools is simpler and faster. This
system doesn't do it, for four reasons that all matter more than the extra hop:

1. **Blast radius.** Core banking credentials are mounted into the MCP pods
   only. The agent tier holds no such secret. `infra/k8s/network-policy.yaml`
   allows egress to the core banking CIDR from `tier: mcp` and denies it
   everywhere else.

2. **Independent scaling.** Tool-call volume and chat volume don't correlate.
   One customer asking "what did I spend on groceries" generates one chat
   request and several transaction queries. The tiers scale separately.

3. **Independent deployment.** Adding a tool means deploying an MCP server, not
   redeploying the agents. That decoupling is what makes a tool catalogue
   maintainable by a different team than the one that owns the graph.

4. **A real audit boundary.** Every tool call crosses a network hop that is
   logged with the calling principal, the arguments, the result status and the
   duration. `app/mcp/client/manager.py` runs the policy engine *before* the
   call, so an unauthorized call never reaches the server.

The three servers map to the diagram's capability groups:

| Server | Port | Tools |
|---|---|---|
| accounts | 8081 | `list_accounts`, `get_balance`, `get_account_details` |
| transactions | 8082 | `search_transactions`, `get_transaction`, `request_statement`, `summarise_spending` |
| service | 8083 | `search_knowledge_base`, `stage_address_change`, `stage_cheque_book_request`, `start_kyc_update`, `confirm_service_request` |

Two details that matter more than they look:

**`customer_id` is never a model-supplied argument.** The client manager injects
it from the verified JWT principal before dispatch. A model that hallucinates
`CUST-9999` produces an argument that is overwritten, not honoured. Belt and
braces: `enforce_tool()` also raises if the argument doesn't match a customer
the principal owns.

**Staging and confirming are separate tools.** `stage_address_change` writes
nothing to core banking; it validates and returns a preview. Only
`confirm_service_request` mutates, and no customer role can call it — it's
reachable only after an operator approves. The model literally cannot complete
a mutation on its own.

Local development runs the same servers as separate processes over streamable
HTTP, so the transport path is exercised in dev exactly as in prod.

---

## The ingestion pipeline

The single most important property: **the upload endpoint does no work.**

```
POST /ingestion/documents
   ├─ validate content type, size
   ├─ hash the bytes (sha256)
   ├─ if the hash already exists for this tenant → return the existing document
   ├─ PUT to S3        (server-side encrypted, versioned)
   ├─ INSERT one row   (status = queued)
   ├─ INSERT one outbox event   ← same transaction
   └─ 202 Accepted
```

Median latency is a few hundred milliseconds whether the file is 10 KB or
200 MB, because parsing and embedding never happen on an API pod. The API tier
stays responsive to chat traffic no matter what someone uploads.

The outbox row rather than a direct enqueue is the detail that keeps this
honest. If the transaction rolls back, the job was never published; if it
commits, the job is guaranteed to be published. There is no window where a
queued job references a document row that doesn't exist.

Then the arq worker (`app/workers/tasks.py`):

```
fetch from S3
   → parse      Docling if available, else pypdf / python-docx / BeautifulSoup / plain
   → chunk      structure-aware
   → embed      batched, via the gateway
   → persist    Postgres: chunk text + metadata + embedding    ← commit here
   → index      OpenSearch bulk
   → status: completed
```

Postgres commits **before** OpenSearch is touched. If the bulk index fails, the
document sits at `indexed_count < chunk_count` and a re-index rebuilds it from
data already paid for. The reverse ordering would leave vectors in the search
index with no record of where they came from.

Chunking (`app/ingestion/chunker.py`) is structure-aware rather than
fixed-window, and the rules come from watching retrieval fail:

- **Tables are never split.** Half a fee table is worse than no fee table — the
  model will confidently answer from the half it can see.
- **Chunks don't cross heading boundaries.** "Overdrafts" content bleeding into
  "International Transfers" produces answers that mix two policies.
- **The section path is prefixed into the chunk text.** A chunk that reads
  `[Terms > Overdrafts] Fees apply after…` retrieves correctly for "overdraft
  fees" even when the chunk body never repeats the word "overdraft". This one
  change did more for retrieval quality than any amount of tuning.
- Target 512 tokens, 64-token overlap, both configurable.

Chunk IDs are deterministic (`document_id` + index + content hash), so replaying
a job overwrites rather than duplicates. Retries are safe.

Large files skip the API entirely: `POST /ingestion/documents/presign` returns a
presigned S3 URL, the client PUTs directly, then calls the complete endpoint,
which verifies the object landed before queueing.

---

## Retrieval

```
query
  ├─ BM25 search  ──┐
  │  (analyzed with banking synonyms: chequebook/checkbook, KYC/know your customer…)
  ├─ kNN search  ───┼─→ RRF fusion ─→ rerank ─→ compress ─→ context + citations
  │  (HNSW, cosine) │
  └─ metadata filters: tenant, classification ceiling, doc type, tags
```

Both legs run concurrently and are fused with **Reciprocal Rank Fusion** rather
than weighted score blending. RRF uses ranks only, which sidesteps the problem
that BM25 scores and cosine similarities aren't on comparable scales and don't
stay comparable as the corpus grows. Documents found by both engines float to
the top, which is exactly the desired behaviour.

Why both engines at all: banking queries carry exact tokens that dense
retrieval blurs. "What's the fee on a 50-leaf cheque book" needs the literal
"50-leaf" to match. Meanwhile "can I take money out early" needs semantic
matching to reach a passage about early withdrawal penalties. Neither engine
handles both cases well; the union does.

After fusion, a cross-encoder reranks the top candidates (with an LLM listwise
fallback when no reranker endpoint is configured), and extractive compression
drops sentences with no lexical overlap with the query. Compression typically
cuts context tokens by half — that's the single largest cost lever in the
system, and it improves faithfulness too, because there's less irrelevant text
for the model to wander into.

Every context block is numbered and carries a citation manifest, so the answer
can point at chunk IDs and the UI can link back to the source document and page.

A single-leg failure degrades rather than fails. If the kNN leg errors, BM25
results still return, tagged so the degradation is visible in metrics.

---

## The model gateway

Everything flows through LiteLLM (`app/llm/gateway.py`, `infra/litellm/config.yaml`)
under logical names:

| Logical name | Used for | Backing |
|---|---|---|
| `self-hosted-llama-70b` | specialist reasoning, synthesis | vLLM, internal |
| `self-hosted-llama-8b` | intent routing, evaluation judging | vLLM, internal |
| `self-hosted-bge-large` | embeddings (1024-dim) | TEI, internal |
| `bedrock-claude` / `azure-gpt-4o` | fallback only | third party |

Routing is latency-based across healthy self-hosted deployments, with
third-party models as a fallback chain rather than a default. Two things follow
from that. Spend stays predictable, and — more importantly — customer data
stays inside the bank's boundary on the normal path.

When a call *does* fall through to a third-party model,
`REDACT_OUTBOUND_TO_THIRD_PARTY_LLM` forces PII redaction on the outbound
messages first. Card numbers (Luhn-validated, so ticket references aren't
mangled), account numbers, emails, phone numbers and IBANs are replaced with
placeholders, and a per-request vault restores them in the response. The model
sees `[EMAIL_1]`; the customer sees their address back.

Two more things live here because there's exactly one place to put them:

**Semantic cache.** Knowledge-lookup queries are embedded and matched against
recent queries above a similarity threshold. "What's the ATM limit" and "how
much can I withdraw from an ATM" hit the same cached answer. Only applied to
read-only knowledge intents — never to anything touching account state, where a
stale answer is a defect.

**Cost ledger.** Every call records tokens, latency, model, deployment tier and
computed cost. Writes are buffered and flushed in batches, because a synchronous
insert per model call adds latency to every turn for data nobody reads in real
time. A Redis counter carries the live daily total for budget enforcement;
Postgres carries the durable record for reporting.

---

## Security

Layered, with each layer assuming the ones outside it may have failed.

**Edge** (`app/api/middleware/edge.py`) — body-size ceiling before the body is
read, WAF-style heuristics, per-principal Redis token bucket with a Lua script
so check-and-decrement is atomic, standard security headers, and a request ID
that propagates into every log line, span and audit row.

**Authentication** (`app/security/auth.py`) — RS256 JWT verified against the
bank's JWKS with a cached key set. Local development can use a shared secret,
and config validation refuses to boot with `DEV_AUTH_BYPASS=true` in prod.

**Authorization** (`app/security/authz.py`, `policies.yaml`) — a fail-closed
policy engine. A tool with no policy entry is denied. Policies bind
role → allowed tools, mark which intents require approval, and set a data
classification ceiling per role. Ownership is checked separately from
permission: having the `get_balance` capability doesn't let you read someone
else's balance.

**Guardrails** (`app/security/guardrails.py`) — regex and heuristic checks on
input and output. Deterministic on purpose: a model-based guardrail is another
thing that can be talked out of its instructions.

**PII** (`app/security/pii.py`) — Presidio when installed, a well-tested regex
fallback otherwise. Two modes: irreversible redaction for logs and traces, and
reversible tokenization for outbound model calls.

**Network** — default-deny NetworkPolicies. The agent tier can reach the MCP
tier, the datastores and the gateway; it cannot reach core banking. Only the MCP
tier can.

Sensitive data is stripped again at the collector
(`infra/otel-collector.yaml` deletes prompt, completion and SQL attributes) on
the assumption that something upstream will eventually leak.

---

## Human in the loop

Address changes, cheque book requests and KYC updates never complete
autonomously.

```
customer: "I've moved to 55 Market Street, Philadelphia PA 19106"

  service agent → stage_address_change   (validates, returns a preview,
                                          writes nothing to core banking)
  approval node → interrupt()            (state persisted to Postgres)
  API          → 202 { status: "pending_approval", approval_id: … }

  ── minutes or days later ──

  operator     → GET  /chat/approvals
  operator     → POST /chat/approvals/{id} { "decision": "approved" }
  graph        → resumes inside the approval node, state intact
  service agent→ confirm_service_request  (now, and only now, it mutates)
```

The pause is durable because it's a checkpoint, not a variable held in memory.
Nothing is rebuilt on resume and no tool call is repeated. Approval requests
carry a risk score, the staged payload, who requested it, and an expiry.

`ENABLE_HUMAN_IN_THE_LOOP=false` collapses the gate for load testing. It should
never be false in production, and the config validator says so.

---

## Observability and cost

The diagram's observability box asks for two different things — model-level
telemetry and machine-level telemetry — and both are here.

**Traces.** One span per graph node, per tool call, per model call, per
retrieval leg. Attributes cover intent, agent, model, deployment tier, token
counts and tool names; prompt and completion bodies are never attached. Tail
sampling keeps all errors and everything slower than three seconds, plus 10% of
the rest.

**Metrics.** Request rate and latency by route, per-node duration, tool calls by
server and status, retrieval latency split by leg, RRF overlap, model tokens and
cost by deployment, guardrail trips by kind, queue depth, ingestion throughput
and failure rate, and process CPU/memory/disk from the resource monitor.

**Audit.** Every tool invocation, every approval decision, every guardrail block
lands in Postgres with the principal and the trace ID. `GET
/admin/traces/session/{id}` reconstructs a whole conversation as an ordered list
of node executions — the first thing to open when a customer disputes an answer.

**Cost.** `GET /admin/cost/summary` breaks spend down by day, model, deployment
and call type. The number worth watching is the third-party share: if it climbs,
the self-hosted fleet is unhealthy and both spend and data residency are
drifting. There's an alert on it at 30%.

Every alert in `infra/alerts.yml` names a runbook anchor in `docs/RUNBOOK.md`.
An alert without a documented response is just noise.

---

## Evaluation

Deterministic metrics first, because they're free, fast and never flake:

| Metric | How |
|---|---|
| `routing_accuracy` | predicted intent vs gold |
| `tool_correctness` | expected tools ⊆ called tools |
| `citation_coverage` | does an answer with a factual claim carry a citation |
| `context_precision` | fraction of retrieved chunks that overlap the gold answer |
| `answer_similarity` | token F1 against the reference — a regression alarm, not a quality score |
| `faithfulness` | LLM judge: is every claim supported by the retrieved context |
| `answer_relevance` | LLM judge: does it answer the question that was asked |

`python scripts/run_eval.py` replays `app/evaluation/datasets/golden.jsonl`
through the real compiled graph — not a mock — and exits non-zero if any metric
falls below its floor. Wire it into CI as a deploy gate.

The dataset includes adversarial cases (prompt injection, cross-customer
probes) and out-of-scope questions, because routing accuracy on only the happy
path tells you nothing about what happens when someone is trying.

Online evaluation samples a configurable share of live turns and scores what can
be scored without a gold answer: faithfulness and citation coverage. That's the
signal that catches a retrieval regression in production before a customer
complaint does.

---

## Data model

Thirteen tables in the `banking` schema.

| Table | Holds |
|---|---|
| `documents` | one row per uploaded file: S3 location, hash, status, counts |
| `document_chunks` | text, metadata, and a pgvector embedding per chunk |
| `ingestion_events` | per-stage timing and outcome — the pipeline's flight recorder |
| `chat_sessions` / `chat_messages` | durable conversation archive |
| `cost_records` / `daily_budgets` | the spend ledger and its limits |
| `agent_steps` / `tool_invocations` | agent execution trace and tool audit |
| `approval_requests` | the HITL queue |
| `outbox_events` | transactional job publication |
| `eval_runs` / `eval_results` | evaluation history |

Notable indexes: HNSW on `document_chunks.embedding` for the pgvector recall
baseline, GIN on `documents.doc_metadata` for filtered listing, and a unique
constraint on `(tenant_id, content_sha256)` — that constraint is the dedupe.

Redis holds only what's allowed to be lost: hot conversation history,
inter-agent shared state, rate-limit counters, idempotency keys, the semantic
cache and the live spend counter. Everything durable is in Postgres.

---

## Project layout

```
app/
  agents/        graph.py, orchestrator.py, state.py, nodes/, prompts/
  api/           deps.py, middleware/edge.py, v1/{chat,ingestion,admin,evaluation,health}.py
  core/          config.py, constants.py, exceptions.py, logging.py
  db/            models/, repositories/, session.py
  evaluation/    metrics.py, runner.py, datasets/golden.jsonl
  ingestion/     parsers.py, chunker.py, pipeline.py
  llm/           gateway.py, cost_tracker.py, model_registry.py
  mcp/           client/manager.py, servers/{accounts,transactions,service}_server.py
  memory/        checkpointer.py, session_store.py, rate_limiter.py, semantic_cache.py
  observability/ tracing.py, metrics.py, callbacks.py, resource_monitor.py
  schemas/       chat.py, ingestion.py, common.py
  security/      auth.py, authz.py, guardrails.py, pii.py, policies.yaml
  storage/       s3.py
  vector/        hybrid_search.py, reranker.py, compression.py, opensearch_client.py
  workers/       worker.py, tasks.py, queue.py
infra/           litellm/, k8s/, opensearch/, prometheus + alerts + otel + grafana
docs/            ARCHITECTURE.md, API.md, RUNBOOK.md
scripts/         bootstrap_index, seed_knowledge_base, smoke_test, run_eval, make_token
tests/           unit/ (no external deps), integration/ (needs the stack up)
```

---

## Operating it

```bash
make help              # everything below, with descriptions
make up / down / clean
make migrate           # alembic upgrade head
make dev / worker / mcp
make test              # unit tests
make test-integration  # needs the stack running
make check             # lint + types + tests, same as CI
make eval              # offline evaluation suite
make smoke             # end-to-end against a running stack
```

**Deployment.** `infra/k8s/` has the API deployment (HPA on CPU and in-flight
requests, PDB, zone spread, a 30-attempt startup probe because MCP connect plus
graph compile is slow, and a preStop sleep so the load balancer drains first),
the workers (KEDA scaling on Redis queue depth — a backlog of large PDFs doesn't
move CPU on an idle pod, so CPU-based autoscaling would never fire), the MCP
servers, default-deny NetworkPolicies, and config wired to an external secret
store.

**Re-embedding without downtime.** Build `kb-chunks-v2` with the new mapping,
backfill it from `document_chunks` in Postgres, then
`POST /admin/index/swap-alias` to repoint `kb-chunks` atomically. No re-embed,
no read downtime.

**Adding a capability.** Add the tool to the relevant MCP server, add a policy
entry in `policies.yaml` (fail-closed means it's denied until you do), add the
intent to `constants.py` if it needs one, and add golden cases. The graph itself
usually doesn't change.

---

## What's deliberately unfinished

Being straight about it, because "production-grade" doesn't mean "finished":

- **Multi-intent messages** get handled one intent at a time. The state has an
  unused `fanout` channel where parallel intent handling would go.
- **Streaming is node-level, not token-level.** The SSE endpoint emits progress
  events as the graph advances rather than streaming tokens from synthesis.
  Token streaming through a checkpointed graph is doable and wasn't done here.
- **OCR isn't wired up.** Scanned PDFs parse to empty text and are quarantined
  rather than silently indexed as blank. Docling's OCR backend is the hook.
- **The cross-encoder falls back to an LLM listwise reranker** when no reranker
  endpoint is configured, which is slower and costs tokens.
- **No step-up authentication.** A high-risk action should arguably re-challenge
  the customer, not just route to an operator.
- **Core banking is synthetic.** `app/mcp/servers/core_banking.py` is a
  deterministic seeded fixture. It's the single seam to replace for a real
  integration, and it's isolated behind the MCP servers precisely so that
  replacing it touches nothing else.
