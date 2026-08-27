"""tiktoken utilities.

Token counting / encode / decode against tiktoken encodings. Useful for sizing
prompts and debugging context budgets. Authenticated but not operator-gated --
it's a pure local computation with no side effects.
"""
from __future__ import annotations

from typing import Any

import tiktoken
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.deps import current_principal
from app.core.logging import get_logger

log = get_logger(__name__)
router = APIRouter(
    prefix="/services/tiktoken",
    tags=["services: tiktoken"],
    dependencies=[Depends(current_principal)],
)

_DEFAULT_ENCODING = "cl100k_base"


def _get_encoding(encoding: str | None, model: str | None):
    try:
        if model:
            return tiktoken.encoding_for_model(model)
        return tiktoken.get_encoding(encoding or _DEFAULT_ENCODING)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"Unknown encoding/model: {exc}") from exc


class TextRequest(BaseModel):
    text: str = Field(max_length=100_000)
    encoding: str | None = None
    model: str | None = None


class TokensRequest(BaseModel):
    tokens: list[int] = Field(min_length=1, max_length=100_000)
    encoding: str | None = None
    model: str | None = None


@router.get("/encodings", summary="List available encodings")
async def list_encodings() -> dict[str, Any]:
    return {"encodings": tiktoken.list_encoding_names(), "default": _DEFAULT_ENCODING}


@router.post("/count", summary="Count tokens in text")
async def count_tokens(body: TextRequest) -> dict[str, Any]:
    enc = _get_encoding(body.encoding, body.model)
    tokens = enc.encode(body.text)
    return {"encoding": enc.name, "characters": len(body.text), "token_count": len(tokens)}


@router.post("/encode", summary="Encode text to token ids")
async def encode_text(body: TextRequest) -> dict[str, Any]:
    enc = _get_encoding(body.encoding, body.model)
    tokens = enc.encode(body.text)
    return {"encoding": enc.name, "token_count": len(tokens), "tokens": tokens}


@router.post("/decode", summary="Decode token ids to text")
async def decode_tokens(body: TokensRequest) -> dict[str, Any]:
    enc = _get_encoding(body.encoding, body.model)
    try:
        text = enc.decode(body.tokens)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Decode failed: {exc}") from exc
    return {"encoding": enc.name, "text": text}
