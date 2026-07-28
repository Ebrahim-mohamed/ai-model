from abc import ABC, abstractmethod


class LLMInterface(ABC):
    """Port every embedding-model adapter (BGEM3Provider today, a future
    real Swan-Large or other candidate tomorrow) must implement.
    ChunkingController and RetrievalController only ever call
    embed_text() against this interface — never a specific model class
    (claude.md §1.2) — so promoting a shootout winner to production is a
    single EMBEDDING_BACKEND config change."""

    @abstractmethod
    async def embed_text(self, texts: list[str], is_query: bool = False) -> list[list[float]]:
        """Embeds a batch of texts into dense vectors. is_query=True
        applies this provider's own query-side instruction/prefix
        convention (if it has one) instead of its document-side
        convention — callers never need to know which convention a given
        model uses, or whether it has one at all."""
        pass

    @property
    @abstractmethod
    def embedding_dimension(self) -> int:
        """The dimensionality of vectors this provider produces. Never
        assumed equal across candidates — a shootout compares them in
        each provider's own scratch space, never a shared fixed-width
        DB column (see EmbeddingShootoutController)."""
        pass
