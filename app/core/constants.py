"""Shared enums and constants used across layers."""
from __future__ import annotations

from enum import StrEnum


class AgentName(StrEnum):
    COORDINATOR = "coordinator"
    ACCOUNTS = "accounts"
    TRANSACTIONS = "transactions"
    SERVICE = "service"


class Intent(StrEnum):
    BALANCE_ENQUIRY = "balance_enquiry"
    TRANSACTION_DETAILS = "transaction_details"
    STATEMENT_REQUEST = "statement_request"
    CHANGE_OF_ADDRESS = "change_of_address"
    CHEQUE_BOOK_REQUEST = "cheque_book_request"
    KYC_UPDATE = "kyc_update"
    KNOWLEDGE_LOOKUP = "knowledge_lookup"
    SMALL_TALK = "small_talk"
    OUT_OF_SCOPE = "out_of_scope"


INTENT_TO_AGENT: dict[Intent, AgentName] = {
    Intent.BALANCE_ENQUIRY: AgentName.ACCOUNTS,
    Intent.TRANSACTION_DETAILS: AgentName.TRANSACTIONS,
    Intent.STATEMENT_REQUEST: AgentName.TRANSACTIONS,
    Intent.CHANGE_OF_ADDRESS: AgentName.SERVICE,
    Intent.CHEQUE_BOOK_REQUEST: AgentName.SERVICE,
    Intent.KYC_UPDATE: AgentName.SERVICE,
    Intent.KNOWLEDGE_LOOKUP: AgentName.SERVICE,
    Intent.SMALL_TALK: AgentName.COORDINATOR,
    Intent.OUT_OF_SCOPE: AgentName.COORDINATOR,
}

# Actions that mutate customer records always require an approval gate.
HIGH_RISK_INTENTS = {Intent.CHANGE_OF_ADDRESS, Intent.KYC_UPDATE, Intent.CHEQUE_BOOK_REQUEST}


class DocumentStatus(StrEnum):
    RECEIVED = "received"
    UPLOADED = "uploaded"
    QUEUED = "queued"
    PARSING = "parsing"
    CHUNKING = "chunking"
    EMBEDDING = "embedding"
    INDEXING = "indexing"
    COMPLETED = "completed"
    FAILED = "failed"
    QUARANTINED = "quarantined"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


REDACTION_TOKEN = "[REDACTED:{kind}]"
TRACE_HEADER = "x-request-id"
IDEMPOTENCY_HEADER = "idempotency-key"
SESSION_HEADER = "x-session-id"
