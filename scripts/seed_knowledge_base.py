#!/usr/bin/env python3
"""Seed the knowledge base with sample banking policy documents.

Writes them through the real ingestion path (S3 -> Postgres -> pipeline) so the
seed exercises exactly the same code the API does. Run after `make up`.
"""
from __future__ import annotations

import asyncio
import uuid

from app.core.constants import DocumentStatus
from app.core.logging import configure_logging, get_logger
from app.db.repositories.documents import DocumentRepository
from app.db.session import dispose_engine, session_scope
from app.ingestion.pipeline import IngestionPipeline
from app.memory.redis_client import close_redis
from app.storage.s3 import get_object_store, sha256_of
from app.vector.opensearch_client import close_client, ensure_index

log = get_logger(__name__)
TENANT = "default"

DOCUMENTS: list[tuple[str, str, str]] = [
    (
        "account-terms.md",
        "terms",
        """# Personal Account Terms and Conditions

## 1. Account Balances

Your available balance reflects cleared funds less any pending card
authorisations. Your ledger balance shows cleared funds only. The two figures
differ whenever a card payment has been authorised but not yet settled, which
typically resolves within three business days.

## 2. ATM and Card Limits

The standard daily ATM withdrawal limit is 500 USD per card. Premier customers
have a daily limit of 1,000 USD. Contactless card payments are capped at 100 USD
per transaction. You can request a temporary limit increase through the app; the
increase applies for 24 hours.

## 3. Overdrafts

Arranged overdrafts accrue interest daily at the rate shown on your statement.
Unarranged overdrafts incur a fee of 15 USD per occurrence, capped at 45 USD per
calendar month. We will notify you before charging an unarranged overdraft fee.

## 4. International Transfers

SWIFT transfers to most destinations settle within one to three business days.
Transfers to countries requiring additional compliance screening may take up to
five business days. The transfer fee is 25 USD for the standard service and
40 USD for the same-day service. Currency conversion uses our published rate at
the time of processing.

## 5. Statements

Statements are issued monthly. Electronic statements are available in the app
immediately. Paper statements are posted within five business days of the
statement date. Historic statements going back seven years can be requested at
no charge.
""",
    ),
    (
        "servicing-procedures.md",
        "procedure",
        """# Servicing Procedures

## Change of Address

A change of registered address requires verification before it takes effect.
Once submitted, the change is reviewed by the servicing team within two business
days. We will write to both the old and the new address to confirm. Cards and
cheque books already in transit are not redirected.

## Cheque Book Requests

Cheque books are available on current accounts only. Savings accounts are not
eligible. A 25-leaf book is issued free of charge. A 50-leaf book costs 5.00 USD
and a 100-leaf book costs 8.50 USD. Delivery by post takes five business days.
Branch collection is available the next business day.

## KYC Refresh

We are required to periodically re-verify customer identity. A KYC refresh needs
an unexpired government photo ID and a proof of address dated within the last
three months. Premier customers are additionally asked for a source-of-wealth
declaration. Cases are normally completed within seven business days. Account
access is not restricted while a refresh is in progress unless the deadline
passes.

## Disputed Transactions

A transaction can be disputed within 120 days of the posting date. We will
provide a provisional credit within ten business days while we investigate.
Card scheme rules govern the final outcome.
""",
    ),
    (
        "product-guide.md",
        "product",
        """# Deposit Product Guide

## Everyday Current Account

No monthly fee. Includes a debit card, mobile and online banking, and access to
an arranged overdraft subject to status. No interest is paid on credit balances.

## Rainy Day Savings Account

Variable interest paid monthly at 3.85% APY on balances up to 50,000 USD, and
1.20% APY above that. No withdrawal restrictions. No monthly fee. Interest is
credited on the first business day of each month.

## Premier Segment

Customers holding combined balances above 75,000 USD qualify for the Premier
segment. Benefits include a higher daily ATM limit, waived international
transfer fees on the standard service, and a dedicated servicing line.

## Fixed Term Deposits

Terms of 6, 12, 24 and 36 months. Early withdrawal forfeits 90 days of interest
on terms up to 12 months, and 180 days on longer terms. Minimum deposit 5,000 USD.
""",
    ),
    (
        "faq.md",
        "faq",
        """# Frequently Asked Questions

**How long does it take to change my address?**
Two business days after you submit it, once our team has verified the change.

**Why is my available balance lower than my ledger balance?**
Because one or more card payments have been authorised but not yet settled. The
difference clears within about three business days.

**Can I get a cheque book on my savings account?**
No. Cheque books are issued on current accounts only.

**How long do international transfers take?**
One to three business days for most destinations; up to five where additional
compliance screening applies.

**What is the daily ATM limit?**
500 USD for standard customers and 1,000 USD for Premier customers.

**How far back can I request statements?**
Seven years, at no charge.
""",
    ),
]


async def main() -> None:
    configure_logging(json_output=False)
    await get_object_store().ensure_buckets()
    await ensure_index()

    pipeline = IngestionPipeline()

    for filename, doc_type, content in DOCUMENTS:
        data = content.encode()
        digest = sha256_of(data)

        async with session_scope() as session:
            repo = DocumentRepository(session)
            if await repo.find_by_hash(TENANT, digest):
                log.info("seed.skipped_duplicate", filename=filename)
                continue

        document_id = uuid.uuid4()
        key = get_object_store().build_key(TENANT, str(document_id), filename)
        stored = await get_object_store().put(
            key=key, data=data, content_type="text/markdown",
            metadata={"document_id": str(document_id), "seed": "true"},
        )

        async with session_scope() as session:
            await DocumentRepository(session).create(
                id=document_id,
                tenant_id=TENANT,
                uploaded_by="seed-script",
                filename=filename,
                content_type="text/markdown",
                size_bytes=len(data),
                content_sha256=digest,
                s3_bucket=stored["bucket"],
                s3_key=stored["key"],
                status=DocumentStatus.QUEUED,
                classification="internal",
                doc_metadata={"doc_type": doc_type, "tags": ["seed", doc_type]},
            )

        result = await pipeline.run(document_id)
        log.info(
            "seed.ingested",
            filename=filename,
            status=result.status,
            chunks=result.chunks,
            indexed=result.indexed,
        )

    await close_client()
    await close_redis()
    await dispose_engine()
    log.info("seed.complete", documents=len(DOCUMENTS))


if __name__ == "__main__":
    asyncio.run(main())
