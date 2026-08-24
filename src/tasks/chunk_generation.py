import asyncio
import logging

from celery_app import celery_app, get_setup_utils

from controllers.ChunkingController import ChunkingController

logger = logging.getLogger(__name__)


@celery_app.task(
    bind=True,
    name="tasks.chunk_generation.generate_chunks",
    autoretry_for=(Exception,),
    retry_kwargs={"max_retries": 3, "countdown": 60},
)
def generate_chunks(self, client_id: str, source_file: str):
    return asyncio.run(_generate_chunks(self, client_id, source_file))


async def _generate_chunks(task_instance, client_id: str, source_file: str):
    """Runs ChunkingController's two-step process (claude.md §3.8) over
    this file's already-staged Bucket-A rows, then delete-and-reinserts the
    result into knowledge_chunks scoped to (client_id, source_file)."""
    setup = None
    try:
        setup = await get_setup_utils()
        chunk_model = setup["chunk_model"]

        controller = ChunkingController(
            staging_row_model=setup["staging_row_model"],
            schema_registry_model=setup["schema_registry_model"],
            client_config_model=setup["client_config_model"],
        )

        chunks = await controller.chunk_source_file(client_id=client_id, source_file=source_file)

        # Delete-and-reinsert (claude.md §2.3): this run's chunks fully
        # replace the previous run's for this client_id/source_file.
        await chunk_model.delete_chunks_by_source_file(client_id=client_id, source_file=source_file)

        inserted = 0
        if chunks:
            inserted = await chunk_model.insert_many_chunks(client_id=client_id, chunks=chunks)

        logger.info(f"client_id={client_id!r} source_file={source_file!r}: generated {inserted} chunks")

        from tasks.embedding_generation import generate_embeddings
        embedding_task = generate_embeddings.delay(client_id=client_id, source_file=source_file)

        return {
            "client_id": client_id,
            "source_file": source_file,
            "chunks_generated": inserted,
            "embedding_task_id": embedding_task.id,
        }

    finally:
        if setup and setup.get("db_engine"):
            await setup["db_engine"].dispose()
