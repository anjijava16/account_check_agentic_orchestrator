"""Initial schema: extensions, schema, and all core tables.

Revision ID: 0001
Revises:
Create Date: 2026-01-01
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

EMBED_DIM = 1024


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS banking")
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"')
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # ------------------------------------------------------------ documents
    op.create_table(
        "documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("uploaded_by", sa.String(128), nullable=False),
        sa.Column("filename", sa.String(512), nullable=False),
        sa.Column("content_type", sa.String(128), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("s3_bucket", sa.String(255), nullable=False),
        sa.Column("s3_key", sa.String(1024), nullable=False),
        sa.Column("s3_version_id", sa.String(255)),
        sa.Column("processed_s3_key", sa.String(1024)),
        sa.Column("status", sa.String(32), nullable=False, server_default="received"),
        sa.Column("failure_reason", sa.Text()),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("parser_backend", sa.String(32)),
        sa.Column("page_count", sa.Integer()),
        sa.Column("chunk_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("embedded_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("indexed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("index_name", sa.String(255)),
        sa.Column("classification", sa.String(32), nullable=False, server_default="internal"),
        sa.Column("doc_metadata", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("ingested_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("tenant_id", "content_sha256", name="uq_documents_tenant_sha"),
        schema="banking",
    )
    op.create_index("ix_documents_status_created", "documents", ["status", "created_at"], schema="banking")
    op.create_index("ix_documents_tenant_id", "documents", ["tenant_id"], schema="banking")
    op.create_index(
        "ix_documents_metadata_gin", "documents", ["doc_metadata"],
        postgresql_using="gin", schema="banking",
    )

    op.create_table(
        "document_chunks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("page_number", sa.Integer()),
        sa.Column("section_path", sa.String(1024)),
        sa.Column("heading", sa.String(512)),
        sa.Column("embedding_model", sa.String(128)),
        sa.Column("embedding", Vector(EMBED_DIM)),
        sa.Column("opensearch_id", sa.String(128)),
        sa.Column("chunk_metadata", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["banking.documents.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("document_id", "chunk_index", name="uq_chunk_doc_index"),
        sa.CheckConstraint("token_count > 0", name="ck_document_chunks_token_count_positive"),
        schema="banking",
    )
    op.create_index("ix_chunks_tenant", "document_chunks", ["tenant_id"], schema="banking")
    op.create_index("ix_chunks_document", "document_chunks", ["document_id"], schema="banking")
    # HNSW on cosine: fast approximate recall for the pgvector side. Built after
    # the table so bulk loads aren't slowed by index maintenance.
    op.execute(
        "CREATE INDEX ix_chunks_embedding_hnsw ON banking.document_chunks "
        "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"
    )

    op.create_table(
        "ingestion_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("stage", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column("detail", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("trace_id", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["banking.documents.id"], ondelete="CASCADE"),
        schema="banking",
    )
    op.create_index("ix_events_document", "ingestion_events", ["document_id"], schema="banking")

    # ----------------------------------------------------------------- chat
    op.create_table(
        "chat_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("channel", sa.String(32), nullable=False, server_default="web"),
        sa.Column("title", sa.String(512)),
        sa.Column("status", sa.String(24), nullable=False, server_default="active"),
        sa.Column("message_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_cost_usd", sa.Numeric(12, 6), nullable=False, server_default="0"),
        sa.Column("last_activity_at", sa.DateTime(timezone=True)),
        sa.Column("session_metadata", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        schema="banking",
    )
    op.create_index("ix_sessions_user_active", "chat_sessions", ["user_id", "last_activity_at"], schema="banking")

    op.create_table(
        "chat_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("redacted_content", sa.Text()),
        sa.Column("agent", sa.String(32)),
        sa.Column("intent", sa.String(48)),
        sa.Column("model", sa.String(128)),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("latency_ms", sa.Integer()),
        sa.Column("citations", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("trace_id", sa.String(64)),
        sa.Column("message_metadata", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["banking.chat_sessions.id"], ondelete="CASCADE"),
        schema="banking",
    )
    op.create_index("ix_messages_session_seq", "chat_messages", ["session_id", "sequence"], schema="banking")

    # ----------------------------------------------------------------- cost
    op.create_table(
        "cost_records",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.String(128)),
        sa.Column("session_id", postgresql.UUID(as_uuid=True)),
        sa.Column("request_id", sa.String(64)),
        sa.Column("usage_date", sa.Date(), nullable=False),
        sa.Column("call_type", sa.String(24), nullable=False),
        sa.Column("agent", sa.String(32)),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("provider", sa.String(64)),
        sa.Column("deployment", sa.String(64)),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cached_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=False, server_default="0"),
        sa.Column("latency_ms", sa.Integer()),
        sa.Column("success", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("cost_metadata", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        schema="banking",
    )
    op.create_index("ix_cost_tenant_day", "cost_records", ["tenant_id", "usage_date"], schema="banking")
    op.create_index("ix_cost_session", "cost_records", ["session_id"], schema="banking")

    op.create_table(
        "daily_budgets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("usage_date", sa.Date(), nullable=False),
        sa.Column("limit_usd", sa.Numeric(12, 2), nullable=False),
        sa.Column("spent_usd", sa.Numeric(12, 6), nullable=False, server_default="0"),
        sa.Column("alert_sent", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("tenant_id", "usage_date", name="uq_budget_tenant_day"),
        schema="banking",
    )

    # --------------------------------------------------------------- traces
    op.create_table(
        "agent_steps",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("turn_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("step_index", sa.Integer(), nullable=False),
        sa.Column("node", sa.String(64), nullable=False),
        sa.Column("agent", sa.String(32)),
        sa.Column("status", sa.String(24), nullable=False, server_default="ok"),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column("prompt_preview", sa.Text()),
        sa.Column("output_preview", sa.Text()),
        sa.Column("trace_id", sa.String(64)),
        sa.Column("span_id", sa.String(64)),
        sa.Column("step_metadata", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        schema="banking",
    )
    op.create_index("ix_steps_session_seq", "agent_steps", ["session_id", "step_index"], schema="banking")

    op.create_table(
        "tool_invocations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("session_id", postgresql.UUID(as_uuid=True)),
        sa.Column("turn_id", postgresql.UUID(as_uuid=True)),
        sa.Column("agent", sa.String(32)),
        sa.Column("mcp_server", sa.String(64), nullable=False),
        sa.Column("tool_name", sa.String(128), nullable=False),
        sa.Column("arguments", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("result_preview", sa.Text()),
        sa.Column("status", sa.String(24), nullable=False, server_default="ok"),
        sa.Column("error", sa.Text()),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column("trace_id", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        schema="banking",
    )
    op.create_index("ix_tools_name_created", "tool_invocations", ["tool_name", "created_at"], schema="banking")

    # ------------------------------------------------------------- approvals
    op.create_table(
        "approval_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("thread_id", sa.String(128), nullable=False),
        sa.Column("checkpoint_id", sa.String(128)),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("requested_by", sa.String(128), nullable=False),
        sa.Column("intent", sa.String(48), nullable=False),
        sa.Column("action", sa.String(128), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("risk_score", sa.Float()),
        sa.Column("rationale", sa.Text()),
        sa.Column("status", sa.String(24), nullable=False, server_default="pending"),
        sa.Column("decided_by", sa.String(128)),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("decision_note", sa.Text()),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        schema="banking",
    )
    op.create_index("ix_approvals_status_created", "approval_requests", ["status", "created_at"], schema="banking")

    # ---------------------------------------------------------------- outbox
    op.create_table(
        "outbox_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("aggregate_type", sa.String(64), nullable=False),
        sa.Column("aggregate_id", sa.String(128), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        schema="banking",
    )
    op.create_index("ix_outbox_unpublished", "outbox_events", ["published_at", "created_at"], schema="banking")

    # ------------------------------------------------------------------ eval
    op.create_table(
        "eval_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("mode", sa.String(24), nullable=False, server_default="offline"),
        sa.Column("dataset", sa.String(255)),
        sa.Column("git_sha", sa.String(64)),
        sa.Column("model", sa.String(128)),
        sa.Column("status", sa.String(24), nullable=False, server_default="running"),
        sa.Column("total_cases", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("passed_cases", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("aggregate_scores", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        schema="banking",
    )

    op.create_table(
        "eval_results",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("case_id", sa.String(128), nullable=False),
        sa.Column("metric", sa.String(64), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("question", sa.Text()),
        sa.Column("prediction", sa.Text()),
        sa.Column("reference", sa.Text()),
        sa.Column("detail", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["banking.eval_runs.id"], ondelete="CASCADE"),
        schema="banking",
    )
    op.create_index("ix_eval_results_run", "eval_results", ["run_id"], schema="banking")


def downgrade() -> None:
    for table in (
        "eval_results", "eval_runs", "outbox_events", "approval_requests",
        "tool_invocations", "agent_steps", "daily_budgets", "cost_records",
        "chat_messages", "chat_sessions", "ingestion_events", "document_chunks",
        "documents",
    ):
        op.drop_table(table, schema="banking")
    op.execute("DROP SCHEMA IF EXISTS banking CASCADE")
