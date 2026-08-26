# API reference

All endpoints are under `/api/v1` and require `Authorization: Bearer <jwt>`.
Interactive docs at `/docs` outside prod.

Get a local token: `python scripts/make_token.py`

## Chat

### `POST /chat`
```json
{"message": "What's my checking balance?", "session_id": "optional", "locale": "en-GB"}
```
Response:
```json
{
  "session_id": "abc123",
  "answer": "Your Everyday Checking has 4,312.88 USD available...",
  "route": "accounts",
  "status": "ok",
  "citations": [],
  "tool_calls": [{"tool": "accounts.get_balance", "ok": true, "latency_ms": 41}],
  "latency_ms": 1830,
  "cost_usd": 0.0021,
  "trace_id": "9f2c..."
}
```

`status` values: `ok`, `awaiting_approval`, `blocked` (guardrail), `error`.

### `POST /chat/stream`
Server-sent events. Event types: `start`, `node` (one per graph node, carries
route and tool names), `final`, `error`, `end`.

### `POST /chat/approvals`
```json
{"approval_id": "uuid", "decision": "approve"}
```
Resumes a suspended write action. Only the customer who initiated it can decide.

### `DELETE /chat/sessions/{session_id}`
Clears history, shared state and the cost counter.

## Ingestion

### `POST /ingestion/documents` (multipart, 202)
Fields: `file`, `classification` (`public`|`internal`|`confidential`),
`source_system`, `metadata` (JSON string). Header: `Idempotency-Key`.

Returns immediately with `document_id` and `job_id`. Duplicate content (same
sha256 within a tenant) returns the original with `"duplicate": true`.

### `GET /ingestion/jobs/{job_id}`
Stages: `queued → parsing → chunking → embedding → indexing → done`.

### `GET /ingestion/documents`, `DELETE /ingestion/documents/{id}`, `POST /ingestion/documents/{id}/reindex`

## Search

### `POST /search`
```json
{"query": "overdraft fee", "top_k": 8, "filters": {"classification": ["public"]}, "rerank": true}
```
Each hit carries `lexical_rank`, `vector_rank` and `rerank_score`, so you can
see which leg of the hybrid found it.

## Admin

`GET /admin/graph` (Mermaid of the live topology), `/admin/tools` (registered
MCP tools with their policies), `/admin/models`, `/admin/cost/{session_id}`,
`/admin/approvals`, `/admin/kb/stats`.

## Evaluation

`POST /evaluation/runs` starts an offline run; `GET /evaluation/runs/{run_id}`
returns per-case scores and aggregates.

## Errors

```json
{"error": {"code": "rate_limited", "message": "...", "retryable": true,
           "detail": {}, "request_id": "..."}}
```

Codes: `unauthenticated`, `forbidden`, `rate_limited`, `payload_too_large`,
`tool_failed`, `model_unavailable`, `budget_exceeded`, `guardrail_blocked`,
`not_found`, `approval_required`, `internal_error`.
