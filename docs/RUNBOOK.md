# Runbook

## High latency
<a name="high-latency"></a>

**Alert:** `HighTurnLatency` -- p95 turn latency above 8s for 10 minutes.

1. Split the latency by stage:
   ```promql
   histogram_quantile(0.95, sum(rate(llm_call_seconds_bucket[5m])) by (le, model))
   histogram_quantile(0.95, sum(rate(mcp_tool_seconds_bucket[5m])) by (le, tool))
   histogram_quantile(0.95, sum(rate(retrieval_seconds_bucket[5m])) by (le, stage))
   ```
2. **Model-side** -- check `/api/v1/admin/models` and the vLLM fleet. If the
   self-hosted deployment is saturated, LiteLLM should already be falling back;
   confirm with `sum(rate(llm_call_seconds_count[5m])) by (model)`.
3. **Tool-side** -- an MCP server is usually waiting on a core banking system.
   Check that pod's logs before blaming the agent.
4. **Retrieval-side** -- if `stage="rerank"` dominates, the cross-encoder
   endpoint is unset and the LLM reranker is being used. Set `_RERANK_ENDPOINT`
   or disable reranking for the knowledge route as a stopgap.
5. Immediate relief: lower `SEARCH_CANDIDATE_K`, or set agent
   `max_iterations` to 2 in `app/agents/specialists.py` and redeploy.

## Tool failures
<a name="tool-failures"></a>

**Alert:** `ToolFailureRate` -- more than 5% of MCP calls failing.

1. Which tool: `sum(rate(mcp_tool_calls_total{outcome="error"}[5m])) by (server, tool)`
2. `kubectl logs -l app=mcp-<server> --tail=200`
3. If `outcome="denied"` is spiking instead, it is an entitlements problem, not
   an outage -- check whether an IdP change dropped a scope from the token.
   Query: `SELECT tool, count(*) FROM tool_invocations WHERE outcome='denied'
   AND created_at > now() - interval '1 hour' GROUP BY tool;`
4. Rolling back an MCP server is safe and independent of the API.

## Ingestion backlog
<a name="ingestion-backlog"></a>

**Alert:** `IngestionBacklog` -- queue depth above 200 for 15 minutes.

1. `curl -H "$AUTH" $API/api/v1/ingestion/queue`
2. Check worker pods for OOMKills: `kubectl get pods -l app=agentic-worker`.
   A single 300-page PDF can hold ~1GB peak. Raise the memory limit before
   raising replicas.
3. KEDA should be scaling on list length. If it is not, check the ScaledObject.
4. Stuck jobs:
   ```sql
   SELECT id, document_id, stage, attempt, error
   FROM ingestion_jobs
   WHERE status = 'running' AND started_at < now() - interval '30 minutes';
   ```
   Re-drive one with `POST /api/v1/ingestion/documents/{id}/reindex` if it
   already has chunks, or re-upload if parsing never completed.

## Guardrail spike

Usually one of two things: a genuine prompt-injection probe, or a false
positive from a new phrasing.

```sql
SELECT route, count(*) FROM conversation_turns
WHERE created_at > now() - interval '1 hour' GROUP BY route;
```

Check the `guardrail_blocks_total` rule label. If `rule="ungrounded_number"` is
spiking, that is not an attack -- it means a model is inventing figures, and the
right response is to check whether a tool started returning empty results.

## Cost spike

1. `sum(rate(llm_cost_usd_total[1h])) by (model, agent) * 3600`
2. If one agent dominates, look for a tool loop:
   ```sql
   SELECT session_id, count(*) FROM tool_invocations
   WHERE created_at > now() - interval '1 hour'
   GROUP BY session_id ORDER BY 2 DESC LIMIT 10;
   ```
3. Immediate lever: lower `COST_BUDGET_USD_PER_SESSION`. It is read per request,
   so a ConfigMap change plus a rollout is enough.

## Rebuilding the vector index

Zero-downtime path, using the read/write aliases:

1. Create `kb-chunks-v2` with the new mapping (`scripts/bootstrap_opensearch.py`
   after bumping `OPENSEARCH_INDEX`).
2. Point `kb-chunks-write` at v2.
3. Backfill: loop `reindex_from_postgres(document_id)` over every document. No
   embeddings are regenerated unless the embedding model itself changed.
4. Verify counts and spot-check relevance via `POST /api/v1/search`.
5. Move `kb-chunks-read` to v2. Delete v1 after a soak period.

## Rotating the third-party model key

1. Update the secret in Vault; ExternalSecret refreshes within an hour, or force
   it with an annotation bump.
2. Rolling restart the API and worker deployments.
3. No traffic impact: LiteLLM fails over to self-hosted while the key is
   invalid, which will show up as a routing shift, not as errors.
