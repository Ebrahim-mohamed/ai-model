import asyncio
import logging

from celery_app import celery_app, get_setup_utils

logger = logging.getLogger(__name__)

_FALLBACK_TOPIC = "unclassified"  # matches NileChat12BBaseProvider's own


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
    outcome_status: str = None,
    resolved_query: str = None,
    retrieval_score: float = None,
    latency_ms: float = None,
    topic_override: str = None,
):
    return asyncio.run(_log_intent_turn(
        client_id, session_id, modality, intent, routing_outcome, resolution_status,
        outcome_status, resolved_query, retrieval_score, latency_ms, topic_override,
    ))


async def _log_intent_turn(
    client_id: str,
    session_id: str,
    modality: str,
    intent: str,
    routing_outcome: str,
    resolution_status: str | None,
    outcome_status: str | None = None,
    resolved_query: str | None = None,
    retrieval_score: float | None = None,
    latency_ms: float | None = None,
    topic_override: str | None = None,
):
    """The one, shared, fire-and-forget write behind Phase 6's Intent
    Log — enqueued from IntentRoutingController.route_turn, never awaited
    inline, so a slow or failed write can never delay or block a
    patient-facing reply (Implementation Plan §Foundation).

    Analytics Dashboard pipeline (2026-09-08): also runs
    QueryRouterInterface.classify_topic here, off the request's own
    critical path — never inline in IntentRoutingController, which would
    add a third blocking LLM call (after rewrite_query/classify_intent)
    to every turn's real, patient-facing latency. Only runs when
    outcome_status is not None — a Mode B or not-yet-implemented turn
    never reached a real Mode A search attempt, so there is no meaningful
    topic to classify (extracted_topic stays NULL, matching outcome_status
    itself — see IntentLog's own column comments). `resolved_query`
    (never the raw, unresolved `text`) is what gets classified — same
    already-standalone-query contract classify_intent/rewrite_query
    already follow, for the identical anaphora-resolution reason.

    topic_override (2026-09-08, Dynamic Disambiguation/Clarification
    Flow) — when set (only on a clarification-requested turn —
    TextReplyController._detect_variant_ambiguity already deterministically
    knows the real parent category from what was actually retrieved), used
    directly as extracted_topic and classify_topic is skipped entirely for
    that turn: more reliable (grounded in the real retrieved candidates,
    not a blind query-text guess) and cheaper (zero extra LLM round-trip)
    than letting classify_topic re-derive the same answer independently."""
    setup = None
    try:
        setup = await get_setup_utils()
        intent_log_model = setup["intent_log_model"]

        extracted_topic = topic_override
        if extracted_topic is None and outcome_status is not None and resolved_query:
            client_config = await setup["client_config_model"].get_client_config(client_id)
            allowed_topics = list((client_config.topic_taxonomy if client_config else None) or [])
            try:
                # No `guidance` — the directive template alone (see
                # NileChat12BBaseProvider.classify_topic) already carries
                # the full instruction; unlike classify_intent, there's no
                # separate worked-examples guidance template for this call
                # yet. Add one the same way whatsapp_intent_classification_
                # guidance exists for classify_intent if real traffic ever
                # shows this needs it.
                extracted_topic = await setup["query_router_client"].classify_topic(
                    resolved_query, allowed_topics,
                )
            except Exception as e:
                # classify_topic's own contract is never-raise for a
                # request-level failure — this except is a further,
                # deliberately broad safety net specific to this
                # fire-and-forget task: a classification-layer bug must
                # never turn into a lost intent_log row (better an
                # unclassified topic than no row at all).
                logger.warning(f"client_id={client_id!r} session_id={session_id!r}: classify_topic failed ({e}) — using {_FALLBACK_TOPIC!r}")
                extracted_topic = _FALLBACK_TOPIC

        await intent_log_model.log_turn(
            client_id=client_id,
            session_id=session_id,
            modality=modality,
            intent=intent,
            routing_outcome=routing_outcome,
            resolution_status=resolution_status,
            outcome_status=outcome_status,
            extracted_topic=extracted_topic,
            retrieval_score=retrieval_score,
            latency_ms=latency_ms,
        )
        logger.info(
            f"client_id={client_id!r} session_id={session_id!r}: logged intent={intent!r} "
            f"routing_outcome={routing_outcome!r} outcome_status={outcome_status!r} "
            f"extracted_topic={extracted_topic!r} retrieval_score={retrieval_score!r} "
            f"latency_ms={latency_ms!r}"
        )
        return {"logged": True}

    finally:
        if setup and setup.get("db_engine"):
            await setup["db_engine"].dispose()
