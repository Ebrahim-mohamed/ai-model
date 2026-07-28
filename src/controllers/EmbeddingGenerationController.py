import logging

from .BaseController import BaseController

logger = logging.getLogger(__name__)


class EmbeddingGenerationController(BaseController):
    """Generates and persists dense embeddings for chunks that don't have
    one yet, using whichever embedding provider is injected (production
    selection is settings.EMBEDDING_BACKEND via LLMProviderFactory — this
    controller never constructs its own provider). Writes happen
    incrementally, one batch at a time — never one giant all-or-nothing
    write at the end. If interrupted partway, already-embedded chunks are
    never lost, and re-running only ever processes what's still missing
    (embedding IS NULL is the sole selection criterion, never a
    positional offset that could skip or repeat rows)."""

    def __init__(self, chunk_model, embedding_client, batch_size: int = 16):
        super().__init__()
        self.chunk_model = chunk_model
        self.embedding_client = embedding_client
        self.batch_size = batch_size

    async def embed_chunks(self, client_id: str, source_file: str | None = None) -> int:
        """source_file scopes this to exactly the batch a sync/chunking
        run just produced (the automatic post-sync path). Omitting it
        processes every chunk still missing an embedding for this
        client — the backfill path — with no other code difference."""
        chunks = await self.chunk_model.get_chunks_without_embedding(client_id=client_id, source_file=source_file)
        if not chunks:
            logger.info(f"client_id={client_id!r} source_file={source_file!r}: no chunks missing an embedding")
            return 0

        total = len(chunks)
        embedded = 0

        for i in range(0, total, self.batch_size):
            batch = chunks[i:i + self.batch_size]
            texts = [chunk.content for chunk in batch]

            # is_query=False — these are document/passage texts, never
            # the query-side of an embedding model's prefix convention.
            vectors = await self.embedding_client.embed_text(texts, is_query=False)

            embeddings = {chunk.id: vector for chunk, vector in zip(batch, vectors)}
            await self.chunk_model.update_embeddings(client_id=client_id, embeddings=embeddings)

            embedded += len(batch)
            logger.info(f"client_id={client_id!r} source_file={source_file!r}: embedded {embedded}/{total} chunks so far")

        return embedded
