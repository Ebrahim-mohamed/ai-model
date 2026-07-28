import logging

import numpy as np

from .BaseController import BaseController

logger = logging.getLogger(__name__)


class EmbeddingShootoutController(BaseController):
    """Orchestrates Step 8's benchmark: embeds this client's real,
    already-chunked content with each candidate model in turn, into that
    model's own in-memory scratch pool — never knowledge_chunks.embedding
    itself, since the production column belongs to whichever model is
    eventually promoted, and candidates aren't guaranteed to share its
    dimensionality (proposal §Step 4: "each writing to its own scratch
    vector index"). Scores top-K retrieval accuracy against a curated,
    real-content-grounded benchmark query set. Orchestration only — every
    actual embedding call goes through LLMInterface.embed_text on
    whichever provider LLMProviderFactory hands back; no embedding math
    of its own beyond the cosine ranking used to score retrieval."""

    def __init__(self, chunk_model, evaluation_query_model, llm_provider_factory):
        super().__init__()
        self.chunk_model = chunk_model
        self.evaluation_query_model = evaluation_query_model
        self.llm_provider_factory = llm_provider_factory

    async def run_shootout(self, client_id: str, model_names: list[str], top_k: int = 5) -> list[dict]:
        queries = await self.evaluation_query_model.get_queries(client_id=client_id)
        if not queries:
            raise ValueError(
                f"No evaluation_queries seeded for client_id={client_id!r} — "
                "seed a benchmark set before running the shootout."
            )

        chunks = await self.chunk_model.get_all_chunks(client_id=client_id)
        if not chunks:
            raise ValueError(f"No knowledge_chunks found for client_id={client_id!r}.")

        chunk_ids = [str(chunk.id) for chunk in chunks]
        chunk_texts = [chunk.content for chunk in chunks]

        results = []
        for model_name in model_names:
            provider = self.llm_provider_factory.create(provider=model_name)

            try:
                chunk_vectors = await self._embed_in_batches(provider, chunk_texts, is_query=False)
                chunk_matrix = np.array(chunk_vectors, dtype=np.float32)

                correct = 0
                for query in queries:
                    query_vector = await provider.embed_text([query.query_text], is_query=True)
                    query_vec = np.array(query_vector[0], dtype=np.float32)

                    # normalize_embeddings=True (every provider's own
                    # _encode) already unit-normalizes both sides, so a
                    # plain dot product IS cosine similarity here.
                    scores = chunk_matrix @ query_vec
                    top_indices = np.argsort(-scores)[:top_k]
                    retrieved_ids = {chunk_ids[i] for i in top_indices}

                    if str(query.expected_chunk_id) in retrieved_ids:
                        correct += 1

                accuracy = correct / len(queries)
                await self.evaluation_query_model.record_result(
                    client_id=client_id, model_name=model_name,
                    top_k_accuracy=accuracy, query_count=len(queries), top_k=top_k,
                )
                logger.info(f"client_id={client_id!r} model={model_name!r}: top_{top_k}_accuracy={accuracy:.3f}")
                results.append({"model_name": model_name, "top_k_accuracy": accuracy, "error": None})

            except Exception as e:
                logger.warning(f"client_id={client_id!r} model={model_name!r} could not run: {e}")
                await self.evaluation_query_model.record_result(
                    client_id=client_id, model_name=model_name,
                    top_k_accuracy=None, query_count=len(queries), top_k=top_k,
                    error_message=str(e),
                )
                results.append({"model_name": model_name, "top_k_accuracy": None, "error": str(e)})

        return results

    async def _embed_in_batches(self, provider, texts: list[str], is_query: bool, batch_size: int = 64) -> list[list[float]]:
        vectors = []
        for i in range(0, len(texts), batch_size):
            batch_vectors = await provider.embed_text(texts[i:i + batch_size], is_query=is_query)
            vectors.extend(batch_vectors)
        return vectors
