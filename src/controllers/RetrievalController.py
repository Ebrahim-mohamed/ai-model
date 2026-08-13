from .BaseController import BaseController


class UnknownClientError(Exception):
    """Raised when the resolved client_id has no client_config row at
    all — should never happen in practice (client_id is only ever
    resolved from a valid admin API key), but the controller never
    assumes a row exists rather than checking."""


class MetadataFilterValidationError(Exception):
    """Raised when metadata_filters contains a key not in this client's
    client_config.allowed_metadata_keys. routes/retrieval.py maps this to
    a validation error response — never an empty/wrong result, and never
    silently ignored (claude.md §3.4)."""


class RetrievalController(BaseController):
    """Orchestrates Step 9's hybrid retrieval: validates metadata_filters
    against this tenant's own allowed_metadata_keys, embeds the query,
    calls stores/vectordb for hybrid (dense+sparse+RRF) search, then
    stores/reranker for the final re-ranking pass. Never touches
    Request/Response objects and never writes raw SQL (claude.md §1.1)."""

    def __init__(self, client_config_model, chunk_model, embedding_client, reranker_client):
        super().__init__()
        self.client_config_model = client_config_model
        self.chunk_model = chunk_model
        self.embedding_client = embedding_client
        self.reranker_client = reranker_client

    async def retrieve(
        self,
        client_id: str,
        query: str,
        metadata_filters: dict | None = None,
        top_k_override: int | None = None,
    ) -> list[dict]:
        client_config = await self.client_config_model.get_client_config(client_id)
        if client_config is None:
            raise UnknownClientError(f"No client_config row for client_id={client_id!r}")

        if metadata_filters:
            allowed_keys = set(client_config.allowed_metadata_keys or [])
            unknown_keys = set(metadata_filters.keys()) - allowed_keys
            if unknown_keys:
                raise MetadataFilterValidationError(
                    f"Unrecognized metadata_filters key(s) for client_id={client_id!r}: "
                    f"{sorted(unknown_keys)}. Allowed keys: {sorted(allowed_keys)}"
                )

        # top_k_override: Section 3 Step 1's query-breadth-aware Mode A
        # needs a per-call top_k (1 for a narrow query, several for a
        # broad one — see TextReplyController) distinct from this
        # client's own /api/retrieve default. Optional and defaulted to
        # None so Section 2's existing retrieval endpoint, which never
        # passes it, is completely unaffected (claude.md §6.1 — additive
        # only, never an edit-in-place to Section 2 behavior).
        top_k = top_k_override if top_k_override is not None else client_config.retrieval_top_k
        rrf_k = client_config.rrf_k
        # How many candidates each of dense/sparse contributes before RRF
        # fusion — a derived multiple of top_k (more candidates than the
        # final result count so fusion has real signal to rank across),
        # not a separate per-client business threshold like top_k/rrf_k
        # themselves (claude.md §1.3 names top-K and RRF's k specifically).
        candidate_k = max(20, top_k * 4)

        query_vectors = await self.embedding_client.embed_text([query], is_query=True)
        query_vector = query_vectors[0]

        fused_chunks = await self.chunk_model.hybrid_search(
            client_id=client_id,
            query_text=query,
            query_vector=query_vector,
            candidate_k=candidate_k,
            rrf_k=rrf_k,
            top_k=candidate_k,  # re-rank over the full fused candidate pool, not just top_k
            metadata_filters=metadata_filters,
        )

        if not fused_chunks:
            return []

        documents = [chunk.content for chunk in fused_chunks]
        reranked = await self.reranker_client.rerank(query=query, documents=documents, top_k=top_k)

        return [
            {"chunk": fused_chunks[index], "score": score}
            for index, score in reranked
        ]
