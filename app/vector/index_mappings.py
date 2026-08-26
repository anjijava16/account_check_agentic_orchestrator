"""OpenSearch index definition for knowledge-base chunks.

Design notes:
  * `content` is analysed for BM25 with an English analyser + a `.keyword`
    subfield for exact phrase filters.
  * `embedding` is a knn_vector using HNSW/cosine -- lucene engine so filtered
    kNN (pre-filter by tenant) works, which is mandatory for multi-tenancy.
  * every metadata field used in a filter is a `keyword`, never `text`.
"""
from __future__ import annotations

from typing import Any

from app.core.config import settings


def build_index_body() -> dict[str, Any]:
    return {
        "settings": {
            "index": {
                "number_of_shards": settings.opensearch_shards,
                "number_of_replicas": settings.opensearch_replicas,
                "refresh_interval": "5s",
                "knn": True,
                "knn.algo_param.ef_search": settings.knn_ef_search,
            },
            "analysis": {
                "filter": {
                    "english_stop": {"type": "stop", "stopwords": "_english_"},
                    "english_stemmer": {"type": "stemmer", "language": "english"},
                    "banking_synonyms": {
                        "type": "synonym_graph",
                        "synonyms": [
                            "chequebook, cheque book, checkbook, check book",
                            "statement, e-statement, account statement",
                            "kyc, know your customer, identity verification",
                            "balance, available balance, account balance",
                            "address, residential address, mailing address",
                        ],
                    },
                },
                "analyzer": {
                    "banking_text": {
                        "type": "custom",
                        "tokenizer": "standard",
                        "filter": [
                            "lowercase",
                            "banking_synonyms",
                            "english_stop",
                            "english_stemmer",
                        ],
                    }
                },
            },
        },
        "mappings": {
            "dynamic": "strict",
            "properties": {
                "chunk_id": {"type": "keyword"},
                "document_id": {"type": "keyword"},
                "tenant_id": {"type": "keyword"},
                "chunk_index": {"type": "integer"},
                "content": {
                    "type": "text",
                    "analyzer": "banking_text",
                    "fields": {"keyword": {"type": "keyword", "ignore_above": 2048}},
                },
                "content_sha256": {"type": "keyword"},
                "title": {"type": "text", "analyzer": "banking_text"},
                "heading": {"type": "text", "analyzer": "banking_text"},
                "section_path": {"type": "keyword"},
                "filename": {"type": "keyword"},
                "page_number": {"type": "integer"},
                "token_count": {"type": "integer"},
                "classification": {"type": "keyword"},
                "doc_type": {"type": "keyword"},
                "language": {"type": "keyword"},
                "source_uri": {"type": "keyword"},
                "tags": {"type": "keyword"},
                "effective_from": {"type": "date"},
                "effective_to": {"type": "date"},
                "created_at": {"type": "date"},
                "embedding_model": {"type": "keyword"},
                "embedding": {
                    "type": "knn_vector",
                    "dimension": settings.embedding_dimensions,
                    "method": {
                        "name": "hnsw",
                        "space_type": "cosinesimil",
                        "engine": "lucene",
                        "parameters": {
                            "m": settings.knn_m,
                            "ef_construction": settings.knn_ef_construction,
                        },
                    },
                },
            },
        },
    }


INDEX_TEMPLATE = {
    "index_patterns": [f"{settings.opensearch_index_prefix}*"],
    "template": build_index_body(),
    "priority": 200,
}
