import asyncio
import logging

from celery_app import celery_app, get_setup_utils

logger = logging.getLogger(__name__)


@celery_app.task(
    bind=True,
    name="tasks.log_intent.log_intent_turn",
    autoretry_for=(Exception,),
    retry_kwargs={"max_retries": 3, "countdown": 10},
)
def log_intent_turn(
    self,
    client_id: str,
    session_id: str,
    modality: str,
    intent: str,
    routing_outcome: str,
    resolution_status: str = None,
):
    return asyncio.run(_log_intent_turn(
        client_id, session_id, modality, intent, routing_outcome, resolution_status,
    ))


async def _log_intent_turn(
    client_id: str,
    session_id: str,
    modality: str,
    intent: str,
    routing_outcome: str,
    resolution_status: str | None,
):
    """The one, shared, fire-and-forget write behind Phase 6's Intent
    Log — enqueued from IntentRoutingController.route_turn, never awaited
    inline, so a slow or failed write can never delay or block a
    patient-facing reply (Implementation Plan §Foundation)."""
    setup = None
    try:
        setup = await get_setup_utils()
        intent_log_model = setup["intent_log_model"]

        await intent_log_model.log_turn(
            client_id=client_id,
            session_id=session_id,
            modality=modality,
            intent=intent,
            routing_outcome=routing_outcome,
            resolution_status=resolution_status,
        )
        logger.info(
            f"client_id={client_id!r} session_id={session_id!r}: logged intent={intent!r} "
            f"routing_outcome={routing_outcome!r}"
        )
        return {"logged": True}

    finally:
        if setup and setup.get("db_engine"):
            await setup["db_engine"].dispose()
