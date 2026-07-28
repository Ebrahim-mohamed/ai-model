import asyncio
import logging

from celery_app import celery_app, get_setup_utils

from controllers.EmbeddingShootoutController import EmbeddingShootoutController
from stores.llm.LLMProviderFactory import LLMProviderFactory

logger = logging.getLogger(__name__)


@celery_app.task(
    bind=True,
    name="tasks.embedding_shootout.run_shootout",
    autoretry_for=(Exception,),
    retry_kwargs={"max_retries": 1, "countdown": 60},
    # Overrides settings.CELERY_TASK_TIME_LIMIT (600s) — a first run also
    # downloads BGE-M3's ~580MB weights and CPU-encodes this client's full
    # real chunk set (batch_size=64), which can comfortably exceed 10
    # minutes on this dev machine's hardware.
    time_limit=1800,
)
def run_shootout(self, client_id: str, top_k: int = 5):
    return asyncio.run(_run_shootout(self, client_id, top_k))


async def _run_shootout(task_instance, client_id: str, top_k: int):
    """Runs EmbeddingShootoutController against every candidate in
    settings.EMBEDDING_BACKEND_LITERAL. A candidate that can't run (e.g.
    Swan-Large's gated-access/hardware blocker — see README's Step 8
    section) is caught and recorded per-model inside the controller, so
    one candidate's failure never prevents the other's real result from
    being scored and persisted."""
    setup = None
    try:
        setup = await get_setup_utils()
        settings = setup["settings"]

        controller = EmbeddingShootoutController(
            chunk_model=setup["chunk_model"],
            evaluation_query_model=setup["evaluation_query_model"],
            llm_provider_factory=LLMProviderFactory(config=settings),
        )

        results = await controller.run_shootout(
            client_id=client_id,
            model_names=settings.EMBEDDING_BACKEND_LITERAL,
            top_k=top_k,
        )

        logger.info(f"client_id={client_id!r}: shootout complete — {results}")
        return {"client_id": client_id, "results": results}

    finally:
        if setup and setup.get("db_engine"):
            await setup["db_engine"].dispose()
