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
# with a role:
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

## 7. Common issues

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

## 8. Shut down

```bash
# Option B: stop host processes (Ctrl-C in each terminal), then stop infra:
docker compose down

# Full reset (delete volumes/data):
docker compose down -v --remove-orphans
```
