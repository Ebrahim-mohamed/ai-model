import asyncio
import logging

from celery_app import celery_app, get_setup_utils

from controllers.EmbeddingGenerationController import EmbeddingGenerationController
from stores.llm.LLMProviderFactory import LLMProviderFactory

logger = logging.getLogger(__name__)


@celery_app.task(
    bind=True,
    name="tasks.embedding_generation.generate_embeddings",
    autoretry_for=(Exception,),
    retry_kwargs={"max_retries": 3, "countdown": 60},
    # Overrides settings.CELERY_TASK_TIME_LIMIT (600s) — CPU-only
    # embedding of a real chunk batch (or a full backfill) can run far
    # longer than that on this dev machine's hardware (see README's
    # Step 8 section on observed encode throughput).
    time_limit=21600,
)
def generate_embeddings(self, client_id: str, source_file: str = None):
    return asyncio.run(_generate_embeddings(self, client_id, source_file))


async def _generate_embeddings(task_instance, client_id: str, source_file: str | None):
    """Chained directly off Step 6's generate_chunks when source_file is
    given — embeds exactly the batch that sync/chunking run just
    produced. Called without source_file for a one-off backfill across
    every chunk still missing an embedding for this client — the same
    task, the same controller, the same selection criterion
    (embedding IS NULL), just a wider scope."""
    setup = None
    try:
        setup = await get_setup_utils()
        settings = setup["settings"]

        embedding_client = LLMProviderFactory(config=settings).create(provider=settings.EMBEDDING_BACKEND)

        controller = EmbeddingGenerationController(
            chunk_model=setup["chunk_model"], embedding_client=embedding_client,
        )

        embedded = await controller.embed_chunks(client_id=client_id, source_file=source_file)

        logger.info(f"client_id={client_id!r} source_file={source_file!r}: embedded {embedded} chunks total")
        return {"client_id": client_id, "source_file": source_file, "chunks_embedded": embedded}

    finally:
        if setup and setup.get("db_engine"):
            await setup["db_engine"].dispose()
