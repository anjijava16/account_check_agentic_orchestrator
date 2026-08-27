"""OpenSearch document CRUD + search.

Operates on document-level primitives (index / get / update / delete / search).
The index name is validated against a strict pattern before it reaches the
client so it can't be used to smuggle path segments into the request URL.
"""
from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from opensearchpy.exceptions import NotFoundError as OSNotFound
from pydantic import BaseModel, Field

from app.api.deps import require_role
from app.core.logging import get_logger
from app.vector.opensearch_client import get_client, index_stats

log = get_logger(__name__)
router = APIRouter(
    prefix="/services/opensearch",
    tags=["services: opensearch"],
    dependencies=[Depends(require_role("agent_operator"))],
)

_INDEX_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,62}$")


def _validate_index(index: str) -> str:
    if not _INDEX_RE.match(index):
        raise HTTPException(status_code=400, detail=f"Invalid index name: {index}")
    return index


class DocumentBody(BaseModel):
    document: dict[str, Any]
    doc_id: str | None = Field(default=None, max_length=512)


class SearchBody(BaseModel):
    query: dict[str, Any] = Field(default_factory=lambda: {"match_all": {}})
    size: int = Field(default=10, ge=1, le=100)
    from_: int = Field(default=0, ge=0, le=10_000, alias="from")

    model_config = {"populate_by_name": True}


@router.get("/indices", summary="List indices with stats")
async def list_indices() -> dict[str, Any]:
    client = get_client()
    indices = await client.cat.indices(format="json")
    return {"count": len(indices), "indices": indices}


@router.post("/{index}/docs", status_code=status.HTTP_201_CREATED, summary="Create a document")
async def create_document(index: str, body: DocumentBody) -> dict[str, Any]:
    _validate_index(index)
    client = get_client()
    result = await client.index(
        index=index, body=body.document, id=body.doc_id, refresh="wait_for"
    )
    return {"index": index, "id": result["_id"], "result": result["result"]}


@router.get("/{index}/docs/{doc_id}", summary="Read a document")
async def read_document(index: str, doc_id: str) -> dict[str, Any]:
    _validate_index(index)
    try:
        result = await get_client().get(index=index, id=doc_id)
    except OSNotFound as exc:
        raise HTTPException(status_code=404, detail=f"Document not found: {doc_id}") from exc
    return {"index": index, "id": result["_id"], "source": result["_source"]}


@router.put("/{index}/docs/{doc_id}", summary="Update (replace) a document")
async def update_document(index: str, doc_id: str, body: DocumentBody) -> dict[str, Any]:
    _validate_index(index)
    result = await get_client().index(
        index=index, id=doc_id, body=body.document, refresh="wait_for"
    )
    return {"index": index, "id": result["_id"], "result": result["result"]}


@router.delete("/{index}/docs/{doc_id}", summary="Delete a document")
async def delete_document(index: str, doc_id: str) -> dict[str, Any]:
    _validate_index(index)
    try:
        await get_client().delete(index=index, id=doc_id, refresh="wait_for")
    except OSNotFound as exc:
        raise HTTPException(status_code=404, detail=f"Document not found: {doc_id}") from exc
    return {"index": index, "id": doc_id, "deleted": True}


@router.post("/{index}/search", summary="Search documents")
async def search_documents(index: str, body: SearchBody) -> dict[str, Any]:
    _validate_index(index)
    result = await get_client().search(
        index=index,
        body={"query": body.query, "size": body.size, "from": body.from_},
    )
    hits = result.get("hits", {})
    return {
        "index": index,
        "total": hits.get("total", {}).get("value", 0),
        "hits": [
            {"id": h["_id"], "score": h.get("_score"), "source": h["_source"]}
            for h in hits.get("hits", [])
        ],
    }


@router.get("/{index}/_stats", summary="Index stats")
async def index_statistics(index: str) -> dict[str, Any]:
    _validate_index(index)
    return await index_stats(index)
