import asyncio
import base64
import logging

from celery_app import celery_app, get_setup_utils

from controllers.DocumentParsingController import DocumentParsingController

logger = logging.getLogger(__name__)


@celery_app.task(
    bind=True,
    name="tasks.document_parsing.parse_and_stage",
    autoretry_for=(Exception,),
    retry_kwargs={"max_retries": 3, "countdown": 60},
)
def parse_and_stage(self, client_id: str, workbook_b64: str, source_file: str = None):
    return asyncio.run(_parse_and_stage(self, client_id, workbook_b64, source_file))


async def _parse_and_stage(task_instance, client_id: str, workbook_b64: str, source_file: str):
    """Parses every sheet in the workbook and stages every row — every
    OneDrive-synced sheet is Bucket A now (architecture override; Bucket
    B/C are static templates entirely decoupled from this pipeline, see
    stores/llm/templates/static/). Nothing is written until the full
    parse succeeds — one sheet failing mandatory-field validation fails
    the whole sync, never a partial write (claude.md §3.1)."""
    setup = None
    try:
        setup = await get_setup_utils()
        schema_registry_model = setup["schema_registry_model"]
        staging_row_model = setup["staging_row_model"]

        parser = DocumentParsingController(schema_registry_model=schema_registry_model)
        workbook_bytes = base64.b64decode(workbook_b64)

        staged_rows = [
            {"sheet_name": sheet_name, "row_data": row_data}
            async for sheet_name, row_data in parser.parse_workbook(client_id=client_id, workbook_bytes=workbook_bytes)
        ]

        # Delete-and-reinsert (claude.md §2.3): this sync's output fully
        # replaces the previous sync's for this client_id/source_file.
        await staging_row_model.delete_rows_by_source_file(client_id=client_id, source_file=source_file)
        if staged_rows:
            await staging_row_model.insert_many_rows(client_id=client_id, rows=staged_rows, source_file=source_file)

        logger.info(f"client_id={client_id!r} source_file={source_file!r}: staged {len(staged_rows)} rows")

        from tasks.chunk_generation import generate_chunks
        chunk_task = generate_chunks.delay(client_id=client_id, source_file=source_file)

        return {
            "client_id": client_id,
            "staged_rows": len(staged_rows),
            "chunk_task_id": chunk_task.id,
        }

    finally:
        if setup and setup.get("db_engine"):
            await setup["db_engine"].dispose()
