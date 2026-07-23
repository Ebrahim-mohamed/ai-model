import asyncio
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
    """Fetches the client's workbook via the OneDrive store. Chained into
    Step 5's parsing task once it exists — for now, this step's job ends at
    'fetch succeeded', since there is nothing yet to dispatch the bytes to."""
    db_engine = None
    try:
        db_engine, client_config_model, token_cache_model, onedrive_client = await get_setup_utils()

        client_config = await client_config_model.get_client_config(client_id)
        if client_config is None or not client_config.onedrive_item_id:
            raise ValueError(f"client_id={client_id!r} has no onedrive_item_id configured in client_config")

        workbook_bytes = await onedrive_client.fetch_file(
            client_id=client_id, item_id=client_config.onedrive_item_id
        )

        logger.info(f"Fetched {len(workbook_bytes)} bytes for client_id={client_id!r}")

        return {"client_id": client_id, "bytes_fetched": len(workbook_bytes)}

    finally:
        if db_engine:
            await db_engine.dispose()
