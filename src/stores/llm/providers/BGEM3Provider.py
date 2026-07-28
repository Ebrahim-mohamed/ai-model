import asyncio

from ..LLMInterface import LLMInterface

# Documented on the BAAI/bge-m3 HuggingFace model card: query texts need
# this instruction prefix prepended, document texts never do. Missing it
# measurably degrades Arabic retrieval precision (proposal §Step 4).
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

# BGE-M3's dense output dimension — matches knowledge_chunks.embedding's
# existing Vector(1024) column with no schema change needed.
EMBEDDING_DIMENSION = 1024


class BGEM3Provider(LLMInterface):
    """BAAI/bge-m3 — general-purpose multilingual embedding model, the
    safe/production-proven candidate. Model weights (~580MB) download
    automatically via sentence-transformers on first use; ungated, no
    HuggingFace account needed."""

    _model = None  # lazy-loaded once per process, shared across instances
    # since SentenceTransformer is thread-safe for inference (proposal
    # §Step 4) — the sync pipeline and a future request handler can share
    # the one loaded model instead of each loading their own copy.

    def _get_model(self):
        if BGEM3Provider._model is None:
            from sentence_transformers import SentenceTransformer
            # low_cpu_mem_usage avoids the transient double-allocation that
            # happens during weight loading (materialize fp32, then copy) —
            # this dev machine's WSL2 RAM (3.7GB total) OOM-killed a real
            # shootout run without it. Pure loading-efficiency, no change
            # in output.
            BGEM3Provider._model = SentenceTransformer(
                "BAAI/bge-m3", model_kwargs={"low_cpu_mem_usage": True},
            )
        return BGEM3Provider._model

    @property
    def embedding_dimension(self) -> int:
        return EMBEDDING_DIMENSION

    async def embed_text(self, texts: list[str], is_query: bool = False) -> list[list[float]]:
        prepared = [f"{QUERY_PREFIX}{t}" for t in texts] if is_query else list(texts)
        return await asyncio.to_thread(self._encode, prepared)

    def _encode(self, texts: list[str]) -> list[list[float]]:
        model = self._get_model()
        # normalize_embeddings=True is not optional — pgvector's cosine
        # distance operator (and this shootout's dot-product scoring)
        # both assume unit-normalized vectors (proposal §Step 4).
        # batch_size=16 (not the proposal's suggested 64) — this dev
        # machine's WSL2 RAM (3.7GB total) is tight enough that a real
        # shootout run OOM-killed at the larger batch size; smaller
        # batches trade some throughput for headroom, never correctness.
        vectors = model.encode(texts, batch_size=16, normalize_embeddings=True, show_progress_bar=False)
        return vectors.tolist()
