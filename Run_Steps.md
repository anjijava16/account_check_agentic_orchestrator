# Run Steps — Local End-to-End

How to run the Agentic Banking Platform locally. There are two supported paths:

- **Option A — Full Docker** (simplest): everything runs in containers via `docker compose`.
- **Option B — Hybrid** (recommended after `uv sync`): infrastructure in Docker, the Python app/worker/MCP servers run on your host with `uv`.

The app config (`app/core/config.py`) already defaults every service URL to `localhost`, so Option B works out of the box against the Dockerized datastores.

---

## 1. Prerequisites

| Tool | Why |
|---|---|
| Docker + Docker Compose | Postgres (pgvector), Redis, OpenSearch, MinIO, LiteLLM, OTel/Jaeger/Prometheus/Grafana |
| `uv` | Python env + runner (you already ran `uv sync`) |
| Python 3.12 | Runtime (provided by the `.venv` uv created) |
| `jq`, `curl` | Optional, for calling the API from the shell |

Verify:

```bash
docker --version
docker compose version
uv --version
```

> Note: There is no `.env.example` in the repo. It is **not required** — `docker-compose.yml` injects env inline, and the app config has working `localhost` defaults. Create a `.env` only if you want to override defaults.

---

## Option A — Full Docker (all-in-one)

Starts 14 containers: API, 2 workers, 3 MCP servers, Postgres, Redis, OpenSearch (+Dashboards), MinIO, LiteLLM, OTel, Jaeger, Prometheus, Grafana.

```bash
# from the repo root
docker compose up -d --build
```

Wait for health (Postgres/OpenSearch/MinIO have healthchecks; the API waits on them and on the migration job):

```bash
docker compose ps
docker compose logs -f api worker      # Ctrl-C to stop tailing
```

Seed sample knowledge-base docs and run the end-to-end smoke test:

```bash
# run inside the API container so it uses the container network + deps
docker compose exec api python scripts/seed_knowledge_base.py
docker compose exec api python scripts/smoke_test.py
```

Tear down:

```bash
docker compose down          # stop
docker compose down -v       # stop + delete all volumes (fresh start)
```

---

## Option B — Hybrid (uv on host + Docker infra)

Best for development because you get reload, breakpoints, and fast iteration.

### B.1 Start only the infrastructure in Docker

```bash
docker compose up -d \
  postgres redis opensearch opensearch-dashboards minio litellm \
  otel-collector jaeger prometheus grafana
```

Confirm the datastores are healthy:

```bash
docker compose ps
```

### B.2 Apply database migrations

```bash
uv run alembic upgrade head
```

### B.3 Create the OpenSearch index

```bash
uv run python scripts/bootstrap_index.py
```

### B.4 Run the app processes — three terminals

**Terminal 1 — MCP tool servers (accounts :8081, transactions :8082, service :8083):**

```bash
uv run python -m app.mcp.servers.accounts_server &
uv run python -m app.mcp.servers.transactions_server &
uv run python -m app.mcp.servers.service_server &
wait
```

**Terminal 2 — ingestion worker (arq):**

```bash
uv run arq app.workers.worker.WorkerSettings
```

**Terminal 3 — API (with reload):**

```bash
uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

> `uv run <cmd>` executes inside the synced `.venv`. Alternatively `source .venv/bin/activate` once, then run the bare commands (`alembic`, `arq`, `uvicorn`, `python …`).

### B.5 Seed knowledge base + smoke test

```bash
uv run python scripts/seed_knowledge_base.py
uv run python scripts/smoke_test.py
```

---

## 2. Verify it's up

| What | URL |
|---|---|
| API docs (Swagger) | http://localhost:8000/docs |
| Health report | http://localhost:8000/health |
| Liveness | http://localhost:8000/live |
| Traces (Jaeger) | http://localhost:16686 |
| Dashboards (Grafana) | http://localhost:3000 — `admin` / `admin` |
| Object store (MinIO) | http://localhost:9001 — `minioadmin` / `minioadmin` |
| Search dashboards (OpenSearch) | http://localhost:5601 |
| Prometheus | http://localhost:9090 |

Quick health check:

```bash
curl -s localhost:8000/health | jq
```

---

## 3. Get a dev token

Local auth bypass is on (`DEV_AUTH_BYPASS=true`, non-prod), so you can mint a token:

```bash
uv run python scripts/make_token.py
# with a role (needed for the /services/* operator endpoints in section 7):
uv run python scripts/make_token.py --sub ops.user --roles customer,agent_operator
```

---

## 4. Run one chat turn

```bash
TOKEN=$(uv run python scripts/make_token.py)

curl -s localhost:8000/api/v1/chat \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"message":"How long does an international transfer take?"}' | jq
```

Other intents to try:

```text
"What's my balance?"                                  → balance_enquiry
"What did I spend on groceries in the last 30 days?"  → transaction_details
"Can I get a statement for last month?"               → statement_request
"I need a new cheque book"                            → cheque_book_request  (staged → approval)
"What's the weather today?"                           → out_of_scope
```

---

## 5. Upload a document (ingestion path)

```bash
TOKEN=$(uv run python scripts/make_token.py)

curl -s -X POST localhost:8000/api/v1/ingestion/documents \
  -H "Authorization: Bearer $TOKEN" \
  -F 'file=@README.md;type=text/markdown' \
  -F 'doc_type=policy' -F 'classification=internal' -F 'tags=demo' | jq
```

Poll status with the returned `document_id`:

```bash
curl -s localhost:8000/api/v1/ingestion/documents/<document_id>/status \
  -H "Authorization: Bearer $TOKEN" | jq
```

Status moves `queued → processing → completed` once the worker embeds and indexes it.

---

## 6. Tests

```bash
uv run pytest tests/unit -v                        # unit tests
uv run pytest tests/integration -v -m integration  # needs the stack running
```

---

## 7. Service routers (operator API) — testing commands

The platform exposes per-service admin/CRUD routers under `/api/v1/services/*`.
Most require the **`agent_operator`** role; a few (`security/token`, `tiktoken`,
`agents`) need only an authenticated token.

> These run inside the image (`pip install .`). After changing router code,
> rebuild: `docker compose up -d --build api`.

**Get an operator token once and reuse it:**

```bash
export OP_TOKEN=$(uv run python scripts/make_token.py --roles customer,agent_operator)
alias acurl='curl -s -H "Authorization: Bearer $OP_TOKEN"'
```

| Router | Role |
|---|---|
| `postgres`, `redis`, `opensearch`, `storage`, `traces`, `mcp`, `llm`, `litellm`, `observability`, `docling` | `agent_operator` |
| `security/token`, `tiktoken`, `agents` | any authenticated user |

### 7.1 Postgres — documents CRUD

```bash
# Create
acurl -X POST localhost:8000/api/v1/services/postgres/documents \
  -H 'Content-Type: application/json' \
  -d '{"filename":"terms.md","content_type":"text/markdown","size_bytes":12,
       "content_sha256":"abc123def456","s3_bucket":"banking-raw-documents","s3_key":"demo/terms.md"}' | jq

# List / Read / Update / Delete
acurl localhost:8000/api/v1/services/postgres/documents | jq
acurl localhost:8000/api/v1/services/postgres/documents/<id> | jq
acurl -X PATCH localhost:8000/api/v1/services/postgres/documents/<id> \
  -H 'Content-Type: application/json' -d '{"status":"completed"}' | jq
acurl -X DELETE localhost:8000/api/v1/services/postgres/documents/<id> | jq
```

### 7.2 Redis — key/value CRUD

```bash
acurl -X POST localhost:8000/api/v1/services/redis/keys \
  -H 'Content-Type: application/json' -d '{"key":"demo:1","value":"hello","ttl_seconds":300}' | jq
acurl localhost:8000/api/v1/services/redis/keys/demo:1 | jq
acurl -X PUT localhost:8000/api/v1/services/redis/keys/demo:1 \
  -H 'Content-Type: application/json' -d '{"value":"updated"}' | jq
acurl 'localhost:8000/api/v1/services/redis/keys?pattern=demo:*' | jq
acurl -X DELETE localhost:8000/api/v1/services/redis/keys/demo:1 | jq
acurl localhost:8000/api/v1/services/redis/_info | jq
```

### 7.3 OpenSearch — document CRUD + search

```bash
acurl localhost:8000/api/v1/services/opensearch/indices | jq
acurl -X POST localhost:8000/api/v1/services/opensearch/kb-chunks/docs \
  -H 'Content-Type: application/json' -d '{"document":{"text":"hello world"},"doc_id":"d1"}' | jq
acurl localhost:8000/api/v1/services/opensearch/kb-chunks/docs/d1 | jq
acurl -X PUT localhost:8000/api/v1/services/opensearch/kb-chunks/docs/d1 \
  -H 'Content-Type: application/json' -d '{"document":{"text":"updated"}}' | jq
acurl -X POST localhost:8000/api/v1/services/opensearch/kb-chunks/search \
  -H 'Content-Type: application/json' -d '{"query":{"match_all":{}},"size":5}' | jq
acurl -X DELETE localhost:8000/api/v1/services/opensearch/kb-chunks/docs/d1 | jq
```

### 7.4 Object store (MinIO) — object CRUD

```bash
acurl -X POST 'localhost:8000/api/v1/services/storage/objects?key=demo/hello.txt' \
  -F 'file=@README.md;type=text/markdown' | jq
acurl 'localhost:8000/api/v1/services/storage/objects?prefix=demo/' | jq
acurl 'localhost:8000/api/v1/services/storage/objects/demo/hello.txt/_meta' | jq
acurl 'localhost:8000/api/v1/services/storage/objects/demo/hello.txt?presign=true' | jq
acurl -X DELETE localhost:8000/api/v1/services/storage/objects/demo/hello.txt | jq
```

### 7.5 Traces (Jaeger)

```bash
acurl localhost:8000/api/v1/services/traces/services | jq
acurl -X POST localhost:8000/api/v1/services/traces/_test-span | jq
acurl 'localhost:8000/api/v1/services/traces?service=agentic-banking-platform&limit=10' | jq
acurl localhost:8000/api/v1/services/traces/<trace_id> | jq
```

### 7.6 MCP servers

```bash
acurl localhost:8000/api/v1/services/mcp/servers | jq
acurl localhost:8000/api/v1/services/mcp/tools | jq
acurl localhost:8000/api/v1/services/mcp/servers/accounts/tools | jq
acurl -X POST localhost:8000/api/v1/services/mcp/servers/accounts/tools/get_balance/invoke \
  -H 'Content-Type: application/json' -d '{"arguments":{}}' | jq
acurl -X POST localhost:8000/api/v1/services/mcp/reconnect | jq
```

### 7.7 LLM test bench (in-process gateway)

```bash
acurl localhost:8000/api/v1/services/llm/models | jq
acurl -X POST localhost:8000/api/v1/services/llm/complete \
  -H 'Content-Type: application/json' -d '{"prompt":"Say hi in 3 words"}' | jq
acurl -X POST localhost:8000/api/v1/services/llm/embed \
  -H 'Content-Type: application/json' -d '{"texts":["hello","world"]}' | jq
```

### 7.8 LiteLLM proxy (container passthrough)

```bash
acurl localhost:8000/api/v1/services/litellm/models | jq
acurl localhost:8000/api/v1/services/litellm/health | jq
acurl -X POST localhost:8000/api/v1/services/litellm/chat \
  -H 'Content-Type: application/json' \
  -d '{"model":"self-hosted-llama-70b","messages":[{"role":"user","content":"ping"}]}' | jq
```

### 7.9 Security — token info + mint (dev only)

```bash
acurl localhost:8000/api/v1/services/security/token | jq
acurl -X POST localhost:8000/api/v1/services/security/token \
  -H 'Content-Type: application/json' -d '{"roles":["customer","agent_operator"]}' | jq
```

### 7.10 tiktoken (any authenticated token)

```bash
acurl -X POST localhost:8000/api/v1/services/tiktoken/count \
  -H 'Content-Type: application/json' -d '{"text":"count these tokens"}' | jq
acurl -X POST localhost:8000/api/v1/services/tiktoken/encode \
  -H 'Content-Type: application/json' -d '{"text":"hello"}' | jq
acurl -X POST localhost:8000/api/v1/services/tiktoken/decode \
  -H 'Content-Type: application/json' -d '{"tokens":[15339]}' | jq
acurl localhost:8000/api/v1/services/tiktoken/encodings | jq
```

### 7.11 Agents info

```bash
acurl localhost:8000/api/v1/services/agents | jq
acurl localhost:8000/api/v1/services/agents/graph | jq
acurl localhost:8000/api/v1/services/agents/service | jq
```

### 7.12 Observability

```bash
acurl localhost:8000/api/v1/services/observability/resources | jq
acurl localhost:8000/api/v1/services/observability/config | jq
acurl localhost:8000/api/v1/services/observability/metrics-names | jq
```

### 7.13 Docling / parser experiments

```bash
acurl localhost:8000/api/v1/services/docling/status | jq
acurl -X POST localhost:8000/api/v1/services/docling/parse \
  -F 'file=@README.md;type=text/markdown' | jq
```

---

## 8. Common issues

| Symptom | Fix |
|---|---|
| `connection refused` to Postgres/Redis/OpenSearch | Datastores not up yet — `docker compose ps`, wait for `healthy`. |
| Migrations fail | Ensure Postgres is healthy, then rerun `uv run alembic upgrade head`. |
| Chat returns a fallback / LLM error | LiteLLM (`infra/litellm/config.yaml`) points at self-hosted vLLM/TEI models that aren't running locally. Retrieval/ingestion still work; wire a local/OpenAI-compatible model into the LiteLLM config to exercise full LLM answers. |
| Worker not picking up uploads | Confirm the arq worker (Terminal 2) is running and Redis is up. |
| MCP tool calls fail | Confirm the three MCP servers (Terminal 1) are listening on 8081/8082/8083. |
| Port already in use | Stop the conflicting process or change the host port mapping in `docker-compose.yml`. |
| OpenSearch container exits | It needs enough memory; ensure Docker has ≥4 GB RAM allocated. |

---

## 9. Shut down

```bash
# Option B: stop host processes (Ctrl-C in each terminal), then stop infra:
docker compose down

# Full reset (delete volumes/data):
docker compose down -v --remove-orphans
```
