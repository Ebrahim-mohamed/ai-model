from abc import ABC, abstractmethod


class RerankerInterface(ABC):
    """Port every re-ranking adapter (CrossEncoderProvider today) must
    implement. Reranking is pure text-relevance scoring over documents
    already fetched and tenant-scoped upstream (RetrievalController /
    VectorDBInterface.hybrid_search) — it never touches storage itself,
    so unlike the vector/OneDrive/LLM ports, client_id has no place in
    this signature (claude.md §1.3 scopes that requirement to functions
    that touch tenant *data*, not pure computation over already-scoped
    inputs)."""

    @abstractmethod
    async def rerank(self, query: str, documents: list[str], top_k: int) -> list[tuple[int, float]]:
        """Returns up to top_k (original_index, score) pairs, sorted by
        score descending — original_index refers to the position of that
        document in the input `documents` list, so the caller can map
        back to whichever object (chunk, metadata, ...) it came from."""
        pass
