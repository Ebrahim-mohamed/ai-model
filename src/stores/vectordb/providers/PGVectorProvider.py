from sqlalchemy import func, select, update

from ..VectorDBInterface import VectorDBInterface
from models.db_schemes.raylab.schemes import KnowledgeChunk


class PGVectorProvider(VectorDBInterface):
    """The concrete adapter for Postgres/pgvector. Every query below is
    scoped by WHERE client_id = :client_id at the SQL level itself —
    never a caller-side filter applied after the fact (claude.md §3.4).
    If this project ever swaps pgvector for a hosted vector DB, this is
    the only file that changes — VectorDBInterface's contract stays put."""

    def __init__(self, db_client):
        self.db_client = db_client

    async def insert_many(self, client_id: str, chunks: list[KnowledgeChunk], batch_size: int = 100) -> int:
        for chunk in chunks:
            chunk.client_id = client_id

        async with self.db_client() as session:
            async with session.begin():
                for i in range(0, len(chunks), batch_size):
                    session.add_all(chunks[i:i + batch_size])
            await session.commit()
        return len(chunks)

    async def search_by_vector(
        self,
        client_id: str,
        query_vector: list[float],
        top_k: int = 5,
        metadata_filters: dict | None = None,
    ) -> list[KnowledgeChunk]:
        async with self.db_client() as session:
            stmt = (
                select(KnowledgeChunk)
                .where(KnowledgeChunk.client_id == client_id)
                .where(KnowledgeChunk.embedding.isnot(None))
            )

            # Exact-match JSONB containment only — never a free-text match
            # against metadata (claude.md §3.4). Validating filter keys
            # against client_config.allowed_metadata_keys is the caller's
            # job (RetrievalController, Step 9) — this adapter just runs
            # whatever containment dict it's handed.
            if metadata_filters:
                stmt = stmt.where(KnowledgeChunk.metadata_payload.contains(metadata_filters))

            stmt = stmt.order_by(KnowledgeChunk.embedding.cosine_distance(query_vector)).limit(top_k)

            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def search_by_bm25(
        self,
        client_id: str,
        query_text: str,
        top_k: int = 5,
        metadata_filters: dict | None = None,
    ) -> list[KnowledgeChunk]:
        """The sparse/keyword leg of hybrid search. Uses Postgres's
        'simple' text-search config deliberately, not 'english' — stock
        Postgres ships no Arabic stemming dictionary, and 'english' would
        incorrectly apply English stemming rules to Arabic tokens.
        'simple' (tokenize + lowercase, no stemming) is the honest choice
        for this client's real content, not a placeholder."""
        async with self.db_client() as session:
            tsvector = func.to_tsvector("simple", KnowledgeChunk.content)
            tsquery = func.plainto_tsquery("simple", query_text)
            rank = func.ts_rank_cd(tsvector, tsquery).label("rank")

            stmt = (
                select(KnowledgeChunk)
                .where(KnowledgeChunk.client_id == client_id)
                .where(tsvector.op("@@")(tsquery))
            )

            if metadata_filters:
                stmt = stmt.where(KnowledgeChunk.metadata_payload.contains(metadata_filters))

            stmt = stmt.order_by(rank.desc()).limit(top_k)

            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def hybrid_search(
        self,
        client_id: str,
        query_text: str,
        query_vector: list[float],
        candidate_k: int,
        rrf_k: int,
        top_k: int,
        metadata_filters: dict | None = None,
    ) -> list[KnowledgeChunk]:
        """Runs the dense and sparse legs independently (each already
        client_id-scoped and metadata-filtered), then fuses them with
        standard Reciprocal Rank Fusion: score(doc) = sum over each leg
        it appears in of 1/(rrf_k + rank_in_that_leg). A chunk appearing
        in only one leg still scores using just that leg's term — never
        penalized with an assumed worst-case rank in the other. If the
        dense leg is empty (no chunks embedded yet for this client — see
        claude.md/README Step 8), fusion degrades gracefully to
        sparse-only ranking, never an error."""
        dense_results = await self.search_by_vector(
            client_id=client_id, query_vector=query_vector, top_k=candidate_k, metadata_filters=metadata_filters,
        )
        sparse_results = await self.search_by_bm25(
            client_id=client_id, query_text=query_text, top_k=candidate_k, metadata_filters=metadata_filters,
        )

        scores: dict = {}
        chunks_by_id: dict = {}

        for leg_results in (dense_results, sparse_results):
            for rank, chunk in enumerate(leg_results, start=1):
                scores[chunk.id] = scores.get(chunk.id, 0.0) + 1.0 / (rrf_k + rank)
                chunks_by_id[chunk.id] = chunk

        fused_ids = sorted(scores, key=lambda chunk_id: scores[chunk_id], reverse=True)[:top_k]
        return [chunks_by_id[chunk_id] for chunk_id in fused_ids]

    async def update_embeddings(self, client_id: str, embeddings: dict) -> int:
        if not embeddings:
            return 0

        async with self.db_client() as session:
            async with session.begin():
                for chunk_id, vector in embeddings.items():
                    stmt = (
                        update(KnowledgeChunk)
                        .where(KnowledgeChunk.id == chunk_id, KnowledgeChunk.client_id == client_id)
                        .values(embedding=vector)
                    )
                    await session.execute(stmt)
            await session.commit()
        return len(embeddings)
