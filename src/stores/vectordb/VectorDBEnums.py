from enum import Enum


class VectorDBEnums(Enum):
    """Closed set of vector DB provider adapters — mapped to a concrete
    class only inside VectorDBProviderFactory."""

    PGVECTOR = "PGVECTOR"


class DistanceMethodEnums(Enum):
    """Closed set of similarity metrics PGVectorProvider can order by.
    COSINE matches idx_chunks_embedding_hnsw's vector_cosine_ops index."""

    COSINE = "COSINE"
