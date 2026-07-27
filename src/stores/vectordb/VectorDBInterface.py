from abc import ABC, abstractmethod


class VectorDBInterface(ABC):
    """Port every vector-DB adapter (PGVectorProvider today, a future
    Pinecone/Qdrant/Weaviate provider tomorrow) must implement. client_id
    is a required, no-default argument on every method (claude.md §1.3,
    §3.4) — it is impossible to call either method without a tenant
    scope, enforced at this signature, not by convention."""

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
