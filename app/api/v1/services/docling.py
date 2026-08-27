"""Docling / parser experiments.

Upload a document and run it through the parser registry to see how it chunks
into structured blocks, which backend handled it (Docling when available, native
otherwise), and a preview of the extracted layout.
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile

from app.api.deps import require_role
from app.core.logging import get_logger
from app.ingestion.parsers import get_parser_registry

log = get_logger(__name__)
router = APIRouter(
    prefix="/services/docling",
    tags=["services: docling"],
    dependencies=[Depends(require_role("agent_operator"))],
)

_MAX_BYTES = 20 * 1024 * 1024


@router.get("/status", summary="Docling availability")
async def status() -> dict[str, Any]:
    registry = get_parser_registry()
    available = registry._docling_available()  # noqa: SLF001
    return {
        "docling_available": available,
        "native_parsers": [p.name for p in registry._native],  # noqa: SLF001
    }


@router.post("/parse", summary="Parse an uploaded document")
async def parse_document(
    file: UploadFile,
    preview_blocks: Annotated[int, Query(ge=1, le=50)] = 5,
) -> dict[str, Any]:
    data = await file.read()
    if len(data) > _MAX_BYTES:
        raise HTTPException(status_code=413, detail="File exceeds 20MB experiment limit")
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")
    registry = get_parser_registry()
    selected = registry.select(
        file.content_type or "application/octet-stream", file.filename or ""
    )
    parsed = registry.parse(
        data, file.filename or "upload", file.content_type or "application/octet-stream"
    )
    block_types: dict[str, int] = {}
    for block in parsed.blocks:
        block_types[block.block_type] = block_types.get(block.block_type, 0) + 1
    return {
        "filename": file.filename,
        "backend_selected": selected.name,
        "backend_used": parsed.backend,
        "page_count": parsed.page_count,
        "title": parsed.title,
        "language": parsed.language,
        "block_count": len(parsed.blocks),
        "block_types": block_types,
        "char_count": len(parsed.text),
        "blocks_preview": [
            {
                "type": b.block_type,
                "page": b.page_number,
                "heading": b.heading,
                "section_path": b.section_path,
                "text": b.text[:280],
            }
            for b in parsed.blocks[:preview_blocks]
        ],
    }
