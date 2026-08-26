"""Structure-aware chunking.

Rules that actually matter in production:
  * never split a table across chunks -- a half table is worse than none
  * carry the section path into every chunk so a retrieved fragment still
    knows which policy section it came from
  * overlap by whole sentences, not characters, so no chunk starts mid-word
  * hard-cap oversized single blocks rather than emitting a 40k-token chunk
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from app.core.config import settings
from app.ingestion.parsers import ParsedBlock, ParsedDocument

SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")


def estimate_tokens(text: str) -> int:
    """Cheap approximation; the exact tokenizer is used only at prompt-build
    time where the cost of loading tiktoken is amortised."""
    return max(1, len(text) // 4)


@dataclass(slots=True)
class Chunk:
    index: int
    content: str
    token_count: int
    page_number: int | None = None
    heading: str | None = None
    section_path: str | None = None
    block_types: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content.encode()).hexdigest()


class StructureAwareChunker:
    def __init__(
        self,
        target_tokens: int | None = None,
        overlap_tokens: int | None = None,
        max_chunks: int | None = None,
    ) -> None:
        self.target = target_tokens or settings.chunk_size_tokens
        self.overlap = overlap_tokens or settings.chunk_overlap_tokens
        self.max_chunks = max_chunks or settings.max_chunks_per_document

    def chunk(self, document: ParsedDocument) -> list[Chunk]:
        chunks: list[Chunk] = []
        buffer: list[ParsedBlock] = []
        buffer_tokens = 0

        def flush() -> None:
            nonlocal buffer, buffer_tokens
            if not buffer:
                return
            chunk = self._materialise(len(chunks), buffer)
            chunks.append(chunk)
            carry = self._overlap_blocks(buffer)
            buffer = list(carry)
            buffer_tokens = sum(estimate_tokens(b.text) for b in buffer)

        for block in document.blocks:
            tokens = estimate_tokens(block.text)

            # Tables are atomic. Emit whatever is buffered, then the table alone.
            if block.block_type == "table":
                flush()
                if tokens > self.target * 3:
                    for part in self._split_oversized(block):
                        chunks.append(self._materialise(len(chunks), [part]))
                else:
                    chunks.append(self._materialise(len(chunks), [block]))
                buffer, buffer_tokens = [], 0
                continue

            # A heading is a natural boundary if we already have enough content.
            if block.block_type == "heading" and buffer_tokens >= self.target * 0.6:
                flush()

            if tokens > self.target * 2:
                flush()
                for part in self._split_oversized(block):
                    chunks.append(self._materialise(len(chunks), [part]))
                buffer, buffer_tokens = [], 0
                continue

            if buffer_tokens + tokens > self.target and buffer:
                flush()

            buffer.append(block)
            buffer_tokens += tokens

            if len(chunks) >= self.max_chunks:
                break

        flush()
        return chunks[: self.max_chunks]

    def _materialise(self, index: int, blocks: list[ParsedBlock]) -> Chunk:
        text = "\n\n".join(b.text.strip() for b in blocks if b.text.strip())
        heading = next((b.heading for b in blocks if b.heading), None)
        section = next((b.section_path for b in blocks if b.section_path), None)
        page = next((b.page_number for b in blocks if b.page_number is not None), None)

        # Prefixing the section path is a small cost that measurably improves
        # both BM25 recall and answer attribution.
        prefixed = f"[{section}]\n{text}" if section else text
        return Chunk(
            index=index,
            content=prefixed,
            token_count=estimate_tokens(prefixed),
            page_number=page,
            heading=heading,
            section_path=section,
            block_types=sorted({b.block_type for b in blocks}),
        )

    def _overlap_blocks(self, blocks: list[ParsedBlock]) -> list[ParsedBlock]:
        if self.overlap <= 0 or not blocks:
            return []
        carried: list[ParsedBlock] = []
        total = 0
        for block in reversed(blocks):
            if block.block_type == "table":
                break
            tokens = estimate_tokens(block.text)
            if total + tokens > self.overlap:
                sentences = SENTENCE_SPLIT.split(block.text)
                tail: list[str] = []
                for sentence in reversed(sentences):
                    if total + estimate_tokens(sentence) > self.overlap:
                        break
                    tail.insert(0, sentence)
                    total += estimate_tokens(sentence)
                if tail:
                    carried.insert(
                        0,
                        ParsedBlock(
                            text=" ".join(tail),
                            page_number=block.page_number,
                            heading=block.heading,
                            section_path=block.section_path,
                        ),
                    )
                break
            carried.insert(0, block)
            total += tokens
        return carried

    def _split_oversized(self, block: ParsedBlock) -> list[ParsedBlock]:
        sentences = SENTENCE_SPLIT.split(block.text) or [block.text]
        parts: list[ParsedBlock] = []
        buffer: list[str] = []
        tokens = 0
        for sentence in sentences:
            sent_tokens = estimate_tokens(sentence)
            if tokens + sent_tokens > self.target and buffer:
                parts.append(
                    ParsedBlock(
                        text=" ".join(buffer), page_number=block.page_number,
                        heading=block.heading, section_path=block.section_path,
                        block_type=block.block_type,
                    )
                )
                buffer, tokens = [], 0
            buffer.append(sentence)
            tokens += sent_tokens
        if buffer:
            parts.append(
                ParsedBlock(
                    text=" ".join(buffer), page_number=block.page_number,
                    heading=block.heading, section_path=block.section_path,
                    block_type=block.block_type,
                )
            )
        return parts
