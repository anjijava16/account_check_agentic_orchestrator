from __future__ import annotations

from app.ingestion.chunker import StructureAwareChunker, estimate_tokens
from app.ingestion.parsers import ParsedBlock, ParsedDocument


def _doc(blocks: list[ParsedBlock]) -> ParsedDocument:
    return ParsedDocument(blocks=blocks, page_count=1, backend="test")


def test_short_document_yields_one_chunk():
    doc = _doc([ParsedBlock(text="Short paragraph about balances.")])
    assert len(StructureAwareChunker().chunk(doc)) == 1


def test_tables_are_never_split():
    table = ParsedBlock(text="\n".join(f"row {i} | value {i}" for i in range(40)), block_type="table")
    chunks = StructureAwareChunker(target_tokens=64).chunk(_doc([table]))
    table_chunks = [c for c in chunks if "table" in c.block_types]
    assert len(table_chunks) >= 1


def test_section_path_is_prefixed_into_content():
    blocks = [
        ParsedBlock(text="Overdrafts", block_type="heading", heading="Overdrafts", section_path="Terms > Overdrafts"),
        ParsedBlock(text="Fees apply.", section_path="Terms > Overdrafts"),
    ]
    chunk = StructureAwareChunker().chunk(_doc(blocks))[0]
    assert "[Terms > Overdrafts]" in chunk.content


def test_long_document_splits_and_respects_target():
    blocks = [ParsedBlock(text="Sentence number %d about policy. " % i * 20) for i in range(30)]
    chunks = StructureAwareChunker(target_tokens=200, overlap_tokens=20).chunk(_doc(blocks))
    assert len(chunks) > 1
    assert all(c.token_count <= 200 * 3 for c in chunks)


def test_chunk_indices_are_contiguous():
    blocks = [ParsedBlock(text="Paragraph %d. " % i * 30) for i in range(20)]
    chunks = StructureAwareChunker(target_tokens=128).chunk(_doc(blocks))
    assert [c.index for c in chunks] == list(range(len(chunks)))


def test_sha_is_stable_for_identical_content():
    doc = _doc([ParsedBlock(text="Identical content here.")])
    a = StructureAwareChunker().chunk(doc)[0]
    b = StructureAwareChunker().chunk(doc)[0]
    assert a.sha256 == b.sha256


def test_token_estimate_is_monotonic():
    assert estimate_tokens("short") < estimate_tokens("a much longer piece of text here")
