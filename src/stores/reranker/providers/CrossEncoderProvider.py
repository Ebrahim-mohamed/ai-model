import asyncio

from ..RerankerInterface import RerankerInterface

# Small, ungated, multilingual (mMARCO — 14 languages including Arabic)
# cross-encoder — deliberately NOT a larger reranker (e.g. BAAI/bge-reranker-v2-m3):
# reranking runs synchronously on every retrieval request's latency path,
# unlike the one-time offline chunking/embedding step, and this dev
# machine's real hardware constraints (see README's Step 8 section) make
# request-time latency a hard concern, not just a nice-to-have.
MODEL_NAME = "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"


class CrossEncoderProvider(RerankerInterface):
    """Real, working, ungated. Model weights (~470MB) download
    automatically via sentence-transformers on first use."""

    _model = None  # lazy-loaded once per process, shared across instances

    def _get_model(self):
        if CrossEncoderProvider._model is None:
            from sentence_transformers import CrossEncoder
            CrossEncoderProvider._model = CrossEncoder(MODEL_NAME)
        return CrossEncoderProvider._model

    async def rerank(self, query: str, documents: list[str], top_k: int) -> list[tuple[int, float]]:
        if not documents:
            return []
        return await asyncio.to_thread(self._rerank, query, documents, top_k)

    def _rerank(self, query: str, documents: list[str], top_k: int) -> list[tuple[int, float]]:
        model = self._get_model()
        pairs = [[query, document] for document in documents]
        scores = model.predict(pairs)
        ranked = sorted(enumerate(scores), key=lambda pair: pair[1], reverse=True)
        return [(index, float(score)) for index, score in ranked[:top_k]]
