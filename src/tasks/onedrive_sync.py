import asyncio
import base64
import logging

from celery_app import celery_app, get_setup_utils

logger = logging.getLogger(__name__)


@celery_app.task(
    bind=True,
    name="tasks.onedrive_sync.fetch_and_dispatch",
    autoretry_for=(Exception,),
    retry_kwargs={"max_retries": 3, "countdown": 60},
)
def fetch_and_dispatch(self, client_id: str):
    return asyncio.run(_fetch_and_dispatch(self, client_id))


async def _fetch_and_dispatch(task_instance, client_id: str):
    """Fetches every .xlsx file in the client's shared OneDrive folder, then
    hands each file's bytes off to Step 5's parsing task independently —
    one parse_and_stage call per file, each scoped by its own file name as
    source_file, so a sync only ever replaces that specific file's
    previously-staged rows (claude.md §2.3's delete-and-reinsert), never
    all of a client's files at once just because one changed.

    Full-mirror deletion pass (added — see claude.md §2.3): delete-and-
    reinsert only ever fires for files OneDrive still returns, so on its
    own it can never shrink the DB — a file removed from OneDrive was
    never dispatched to parse_and_stage/generate_chunks at all, leaving
    its rows/chunks orphaned forever. After this listing completes, diff
    it against every source_file the DB currently knows about for this
    client (union of ChunkModel + StagingRowModel, since either table
    could in principle be the one still holding a stale file) and delete
    both tables' rows for anything OneDrive no longer has. Deliberately
    placed AFTER the fetch loop finishes, inside the same try block: if
    fetch_files_in_folder raises partway through pagination, this method
    never reaches the diff at all — files_dispatched would be an
    incomplete listing, and autoretry_for=(Exception,) retries the whole
    task from scratch rather than risk deleting a file OneDrive still has
    but pagination just hadn't reached yet."""
    setup = None
    try:
        setup = await get_setup_utils()
        client_config_model = setup["client_config_model"]
        onedrive_client = setup["onedrive_client"]
        chunk_model = setup["chunk_model"]
        staging_row_model = setup["staging_row_model"]

        client_config = await client_config_model.get_client_config(client_id)
        if client_config is None or not client_config.onedrive_item_id or not client_config.onedrive_drive_id:
            raise ValueError(
                f"client_id={client_id!r} has no onedrive_drive_id/onedrive_item_id "
                "(the shared sync folder's drive + folder Item ID) configured in client_config"
            )

        from tasks.document_parsing import parse_and_stage

        parse_task_ids = []
        files_dispatched = []
        async for file_name, workbook_bytes in onedrive_client.fetch_files_in_folder(
            client_id=client_id,
            drive_id=client_config.onedrive_drive_id,
            folder_id=client_config.onedrive_item_id,
        ):
            logger.info(f"Fetched {len(workbook_bytes)} bytes for client_id={client_id!r} file={file_name!r}")

            parse_task = parse_and_stage.delay(
                client_id=client_id,
                workbook_b64=base64.b64encode(workbook_bytes).decode("ascii"),
                source_file=file_name,
            )
            parse_task_ids.append(parse_task.id)
            files_dispatched.append(file_name)

        current_files = set(files_dispatched)
        known_files = (
            set(await chunk_model.get_distinct_source_files(client_id))
            | set(await staging_row_model.get_distinct_source_files(client_id))
        )
        stale_files = sorted(known_files - current_files)

        stale_files_removed = []
        for stale_file in stale_files:
            chunks_deleted = await chunk_model.delete_chunks_by_source_file(
                client_id=client_id, source_file=stale_file,
            )
            staging_rows_deleted = await staging_row_model.delete_rows_by_source_file(
                client_id=client_id, source_file=stale_file,
            )
            logger.info(
                f"client_id={client_id!r}: removed stale source_file={stale_file!r} "
                f"(no longer present on OneDrive) — {chunks_deleted} chunks, "
                f"{staging_rows_deleted} staging rows deleted"
            )
            stale_files_removed.append({
                "source_file": stale_file,
                "chunks_deleted": chunks_deleted,
                "staging_rows_deleted": staging_rows_deleted,
            })

        return {
            "client_id": client_id,
            "files_dispatched": files_dispatched,
            "parse_task_ids": parse_task_ids,
            "stale_files_removed": stale_files_removed,
        }

    finally:
        if setup and setup.get("db_engine"):
            await setup["db_engine"].dispose()
