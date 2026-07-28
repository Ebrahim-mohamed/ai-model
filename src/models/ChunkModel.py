from sqlalchemy.future import select
from sqlalchemy import delete, func

from .BaseDataModel import BaseDataModel
from .db_schemes.raylab.schemes import KnowledgeChunk


class ChunkModel(BaseDataModel):
    """Repository for knowledge_chunks. Every method requires client_id —
    no exceptions. Vector-specific operations (bulk insert, similarity
    search) are never reimplemented here as raw SQL — they delegate to
    the injected stores/vectordb adapter (claude.md §1.2, Step 7), so
    swapping pgvector for another vector DB never touches this file."""

    def __init__(self, db_client: object, vectordb_client=None):
        super().__init__(db_client=db_client)
        self.vectordb_client = vectordb_client

    @classmethod
    async def create_instance(cls, db_client: object, vectordb_client=None):
        return cls(db_client, vectordb_client=vectordb_client)

    async def create_chunk(self, client_id: str, chunk: KnowledgeChunk) -> KnowledgeChunk:
        chunk.client_id = client_id
        async with self.db_client() as session:
            async with session.begin():
                session.add(chunk)
            await session.commit()
            await session.refresh(chunk)
        return chunk

    async def insert_many_chunks(self, client_id: str, chunks: list[KnowledgeChunk], batch_size: int = 100) -> int:
        return await self.vectordb_client.insert_many(client_id=client_id, chunks=chunks, batch_size=batch_size)

    async def search_by_vector(
        self,
        client_id: str,
        query_vector: list[float],
        top_k: int = 5,
        metadata_filters: dict | None = None,
    ) -> list[KnowledgeChunk]:
        return await self.vectordb_client.search_by_vector(
            client_id=client_id,
            query_vector=query_vector,
            top_k=top_k,
            metadata_filters=metadata_filters,
        )

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
        return await self.vectordb_client.hybrid_search(
            client_id=client_id,
            query_text=query_text,
            query_vector=query_vector,
            candidate_k=candidate_k,
            rrf_k=rrf_k,
            top_k=top_k,
            metadata_filters=metadata_filters,
        )

    async def update_embeddings(self, client_id: str, embeddings: dict) -> int:
        return await self.vectordb_client.update_embeddings(client_id=client_id, embeddings=embeddings)

    async def get_chunks_without_embedding(self, client_id: str, source_file: str | None = None) -> list[KnowledgeChunk]:
        """Every chunk still missing an embedding — scoped to source_file
        when given (the freshly delete-and-reinserted batch a sync just
        produced), or every un-embedded chunk for this client when not
        (a backfill covering everything synced before an embedding
        backend was promoted). A plain relational read (filtering on
        embedding being NULL, not comparing vectors), so it stays a
        direct repository method rather than delegating to
        vectordb_client — the same reasoning as get_all_chunks."""
        async with self.db_client() as session:
            stmt = select(KnowledgeChunk).where(
                KnowledgeChunk.client_id == client_id,
                KnowledgeChunk.embedding.is_(None),
            )
            if source_file is not None:
                stmt = stmt.where(KnowledgeChunk.source_file == source_file)
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def get_chunk(self, client_id: str, chunk_id):
        async with self.db_client() as session:
            stmt = select(KnowledgeChunk).where(
                KnowledgeChunk.id == chunk_id,
                KnowledgeChunk.client_id == client_id,
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def delete_chunks_by_source_file(self, client_id: str, source_file: str) -> int:
        """The delete side of Section 2's delete-and-reinsert sync strategy."""
        async with self.db_client() as session:
            stmt = delete(KnowledgeChunk).where(
                KnowledgeChunk.client_id == client_id,
                KnowledgeChunk.source_file == source_file,
            )
            result = await session.execute(stmt)
            await session.commit()
        return result.rowcount

    async def get_total_chunks_count(self, client_id: str) -> int:
        async with self.db_client() as session:
            stmt = select(func.count(KnowledgeChunk.id)).where(KnowledgeChunk.client_id == client_id)
            result = await session.execute(stmt)
            return result.scalar_one()

    async def get_all_chunks(self, client_id: str) -> list[KnowledgeChunk]:
        """Every chunk for this client, unfiltered by embedding state —
        Step 8's shootout uses this to build each candidate's own
        in-memory scratch pool (see EmbeddingShootoutController), never
        knowledge_chunks.embedding itself. A plain relational read, not a
        vector operation, so it stays a direct repository method rather
        than delegating to vectordb_client."""
        async with self.db_client() as session:
            stmt = select(KnowledgeChunk).where(KnowledgeChunk.client_id == client_id)
            result = await session.execute(stmt)
            return list(result.scalars().all())
