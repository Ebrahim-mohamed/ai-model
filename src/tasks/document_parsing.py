import asyncio
import base64
import logging

from celery_app import celery_app, get_setup_utils

from models.enums.BucketEnum import BucketEnum
from controllers.DocumentParsingController import DocumentParsingController
from utils.template_file_writer import write_template_file

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
    """Parses every sheet in the workbook, routes each row by its
    (auto-discovered or already-registered) BucketEnum, and persists:
    Bucket A -> staging_rows, Bucket B/C -> generated template files.
    Nothing is written until the full parse succeeds — one sheet failing
    mandatory-field validation fails the whole sync, never a partial write
    (claude.md §3.1)."""
    setup = None
    try:
        setup = await get_setup_utils()
        schema_registry_model = setup["schema_registry_model"]
        staging_row_model = setup["staging_row_model"]

        parser = DocumentParsingController(schema_registry_model=schema_registry_model)
        workbook_bytes = base64.b64decode(workbook_b64)

        bucket_a_rows = []
        bucket_bc_rows = {BucketEnum.PROMPT_TEMPLATE: [], BucketEnum.SYSTEM_DIRECTIVE: []}

        async for bucket, sheet_name, row_data in parser.parse_workbook(client_id=client_id, workbook_bytes=workbook_bytes):
            if bucket == BucketEnum.VECTOR_DB:
                bucket_a_rows.append({"sheet_name": sheet_name, "row_data": row_data})
            else:
                bucket_bc_rows[bucket].append(row_data)

        # Delete-and-reinsert (claude.md §2.3): this sync's output fully
        # replaces the previous sync's for this client_id/source_file —
        # for staging_rows via DELETE+INSERT, for Bucket B/C via the
        # template writer's wholesale file overwrite.
        await staging_row_model.delete_rows_by_source_file(client_id=client_id, source_file=source_file)
        if bucket_a_rows:
            await staging_row_model.insert_many_rows(client_id=client_id, rows=bucket_a_rows, source_file=source_file)

        written_files = []
        for bucket, rows in bucket_bc_rows.items():
            if rows:
                written_files.append(write_template_file(client_id=client_id, bucket=bucket, rows=rows))

        logger.info(
            f"client_id={client_id!r}: staged {len(bucket_a_rows)} Bucket-A rows, "
            f"wrote {len(written_files)} template file(s)"
        )

        from tasks.chunk_generation import generate_chunks
        chunk_task = generate_chunks.delay(client_id=client_id, source_file=source_file)

        return {
            "client_id": client_id,
            "staged_rows": len(bucket_a_rows),
            "template_files_written": written_files,
            "chunk_task_id": chunk_task.id,
        }

    finally:
        if setup and setup.get("db_engine"):
            await setup["db_engine"].dispose()
