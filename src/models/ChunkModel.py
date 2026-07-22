from sqlalchemy.future import select
from sqlalchemy import delete, func

from .BaseDataModel import BaseDataModel
from .db_schemes.raylab.schemes import KnowledgeChunk


class ChunkModel(BaseDataModel):
    """Repository for knowledge_chunks. Every method requires client_id — no exceptions."""

    def __init__(self, db_client: object):
        super().__init__(db_client=db_client)

    @classmethod
    async def create_instance(cls, db_client: object):
        return cls(db_client)

    async def create_chunk(self, client_id: str, chunk: KnowledgeChunk) -> KnowledgeChunk:
        chunk.client_id = client_id
        async with self.db_client() as session:
            async with session.begin():
                session.add(chunk)
            await session.commit()
            await session.refresh(chunk)
        return chunk

    async def insert_many_chunks(self, client_id: str, chunks: list[KnowledgeChunk], batch_size: int = 100) -> int:
        for chunk in chunks:
            chunk.client_id = client_id

        async with self.db_client() as session:
            async with session.begin():
                for i in range(0, len(chunks), batch_size):
                    session.add_all(chunks[i:i + batch_size])
            await session.commit()
        return len(chunks)

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
