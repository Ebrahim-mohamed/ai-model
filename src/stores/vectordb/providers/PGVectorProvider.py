from sqlalchemy import select

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
