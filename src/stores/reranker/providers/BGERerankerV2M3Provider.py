import asyncio

from ..RerankerInterface import RerankerInterface

# 2026-09-02: A/B candidate against CrossEncoderProvider's mmarco-mMiniLMv2 —
# real evidence (Postman traffic) showed that smaller, machine-translated
# model scoring a "غرفة PET CT" (PET-CT room) branch-logistics chunk above
# the actual PET-CT exam/procedure chunk for a query asking about PET-CT
# procedures — a lexical-overlap trap, not a semantic match. BAAI/bge-
# reranker-v2-m3 is built on the same BGE-M3 base this project's own
# embedding model (BAAI/bge-m3) already uses — same architecture lineage,
# real (if not officially itemized) Arabic grounding via XLM-RoBERTa-
# large's own documented pretraining corpus. ~0.6B params vs. the current
# model's much smaller 12-layer MiniLM — a real latency cost, which is
# exactly why this is a togglable A/B candidate (RERANKER_BACKEND config),
# not a replacement: CrossEncoderProvider stays the deployed default until
# this is empirically proven worth that cost on real traffic.
MODEL_NAME = "BAAI/bge-reranker-v2-m3"


class BGERerankerV2M3Provider(RerankerInterface):
    """Real, working, ungated (Apache-family license, ~2.2GB weights,
    download automatically via sentence-transformers on first use). Same
    CrossEncoder interface as CrossEncoderProvider — the only difference
    between the two providers is MODEL_NAME, deliberately: this keeps the
    A/B comparison honest (identical reranking call shape, identical
    scoring/sorting logic), isolating the model swap as the only
    variable."""

    _model = None  # lazy-loaded once per process, shared across instances

    def _get_model(self):
        if BGERerankerV2M3Provider._model is None:
            from sentence_transformers import CrossEncoder
            BGERerankerV2M3Provider._model = CrossEncoder(MODEL_NAME)
        return BGERerankerV2M3Provider._model

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
