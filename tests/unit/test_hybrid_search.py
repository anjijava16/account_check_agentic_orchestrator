"""RRF fusion and compression are pure functions worth pinning down."""
from __future__ import annotations

from app.vector.compression import compress_text
from app.vector.hybrid_search import HybridSearcher, SearchRequest, _filters


def _hit(doc_id: str, content: str = "content") -> dict:
    return {
        "_id": doc_id,
        "_source": {"chunk_id": doc_id, "document_id": "d1", "content": content},
    }


def test_rrf_ranks_documents_found_by_both_engines_first():
    searcher = HybridSearcher.__new__(HybridSearcher)
    bm25 = [_hit("a"), _hit("b"), _hit("c")]
    knn = [_hit("c"), _hit("a"), _hit("d")]
    fused = HybridSearcher._rrf(searcher, bm25, knn)
    assert fused[0].chunk_id in {"a", "c"}
    assert len(fused) == 4


def test_rrf_records_both_ranks():
    searcher = HybridSearcher.__new__(HybridSearcher)
    fused = HybridSearcher._rrf(searcher, [_hit("a")], [_hit("a")])
    assert fused[0].bm25_rank == 1
    assert fused[0].knn_rank == 1


def test_knn_only_document_still_appears():
    searcher = HybridSearcher.__new__(HybridSearcher)
    fused = HybridSearcher._rrf(searcher, [], [_hit("z")])
    assert [h.chunk_id for h in fused] == ["z"]


def test_filters_always_scope_by_tenant():
    clauses = _filters(SearchRequest(query="q", tenant_id="acme"))
    assert {"term": {"tenant_id": "acme"}} in clauses


def test_filters_enforce_classification_ceiling():
    clauses = _filters(SearchRequest(query="q", max_classification="internal"))
    terms = [c for c in clauses if "terms" in c and "classification" in c["terms"]]
    assert "restricted" not in terms[0]["terms"]["classification"]


def test_compression_keeps_relevant_sentences():
    text = (
        "The weather is mild today. The daily ATM withdrawal limit is 500 USD. "
        "Our branches open at nine. Premier customers have a 1000 USD limit."
    )
    out = compress_text("What is the ATM withdrawal limit?", text, max_sentences=2)
    assert "500 USD" in out
    assert len(out) < len(text)


def test_compression_leaves_short_text_alone():
    text = "One sentence only."
    assert compress_text("anything", text) == text
