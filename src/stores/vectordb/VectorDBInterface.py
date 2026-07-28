from abc import ABC, abstractmethod


class VectorDBInterface(ABC):
    """Port every vector-DB adapter (PGVectorProvider today, a future
    Pinecone/Qdrant/Weaviate provider tomorrow) must implement. client_id
    is a required, no-default argument on every method (claude.md §1.3,
    §3.4) — it is impossible to call any method without a tenant scope,
    enforced at this signature, not by convention."""

    @abstractmethod
    async def insert_many(self, client_id: str, chunks: list) -> int:
        """Persists a batch of chunks for this client. Every chunk handed
        in is stamped with this same client_id — never a mix of tenants
        in one call."""
        pass

    @abstractmethod
    async def search_by_vector(
        self,
        client_id: str,
        query_vector: list[float],
        top_k: int = 5,
        metadata_filters: dict | None = None,
    ) -> list:
        """Dense similarity search, scoped strictly to client_id. When
        metadata_filters is given, matching rows must also satisfy a
        JSONB containment match against metadata — never a free-text
        match. Rows with no embedding yet are excluded, never returned
        with an undefined distance."""
        pass

    @abstractmethod
    async def hybrid_search(
        self,
        client_id: str,
        query_text: str,
        query_vector: list[float],
        candidate_k: int,
        rrf_k: int,
        top_k: int,
        metadata_filters: dict | None = None,
    ) -> list:
        """Dense (cosine) + sparse (keyword) search, fused via Reciprocal
        Rank Fusion, scoped strictly to client_id (Step 9). candidate_k
        is how many results each of the dense/sparse legs contributes
        before fusion; rrf_k is RRF's own smoothing constant and top_k is
        the final result count — both are per-tenant client_config
        values (claude.md §1.3), never hardcoded. If a client has no
        embedded chunks yet, the dense leg contributes nothing and the
        result gracefully degrades to keyword-only ranking — never an
        error."""
        pass

    @abstractmethod
    async def update_embeddings(self, client_id: str, embeddings: dict) -> int:
        """Bulk-writes computed vectors back onto existing rows.
        embeddings: {chunk_id: [float, ...], ...}. Every UPDATE is scoped
        by WHERE client_id = :client_id, in addition to the chunk_id —
        never trusts the embeddings dict's keys alone to be tenant-safe."""
        pass
