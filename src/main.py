from fastapi import FastAPI
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from routes import base, sync, retrieval, whatsapp, analytics
from helpers.config import get_settings
from helpers.logging_config import configure_logging
from models.ClientConfigModel import ClientConfigModel
from models.ChunkModel import ChunkModel
from models.ChatHistoryModel import ChatHistoryModel
from models.DialogueStateTemplateMapModel import DialogueStateTemplateMapModel
from models.HumanHandoffQueueModel import HumanHandoffQueueModel
from models.IntentLogModel import IntentLogModel
from controllers.SyncController import SyncController
from controllers.RetrievalController import RetrievalController
from controllers.TextReplyController import TextReplyController
from controllers.IntentRoutingController import IntentRoutingController
from controllers.ReplyVerificationController import ReplyVerificationController
from controllers.AnalyticsController import AnalyticsController
from stores.vectordb.VectorDBProviderFactory import VectorDBProviderFactory
from stores.llm.LLMProviderFactory import LLMProviderFactory
from stores.reranker.RerankerProviderFactory import RerankerProviderFactory
from stores.generation.GenerationProviderFactory import GenerationProviderFactory
from stores.query_router.QueryRouterProviderFactory import QueryRouterProviderFactory
from utils.session_store import SessionStore

app = FastAPI()


async def startup_span():
    settings = get_settings()

    # First, before anything else logs a line — step-by-step RAG pipeline
    # tracing (breadth classification, field selection, final CONTEXT
    # construction) needs the FileHandler attached before any of those
    # controllers run their first request.
    configure_logging(log_file_path=settings.RAG_LOG_FILE_PATH, level_name=settings.RAG_LOG_LEVEL)

    postgres_conn = (
        f"postgresql+asyncpg://{settings.POSTGRES_USERNAME}:{settings.POSTGRES_PASSWORD}"
        f"@{settings.POSTGRES_HOST}:{settings.POSTGRES_PORT}/{settings.POSTGRES_MAIN_DATABASE}"
    )
    # pool_pre_ping: this engine lives for the whole process lifetime — a
    # pooled connection can go stale over hours of uptime (network blip,
    # Postgres restart). Pre-ping transparently discards and reconnects
    # instead of surfacing a "connection reset" error to a live request.
    app.db_engine = create_async_engine(postgres_conn, pool_pre_ping=True)
    app.db_client = sessionmaker(app.db_engine, class_=AsyncSession, expire_on_commit=False)

    app.client_config_model = await ClientConfigModel.create_instance(app.db_client)
    app.sync_controller = SyncController(client_config_model=app.client_config_model)

    # Step 9 — the retrieval path is the first thing that makes the API
    # process itself a stores/ consumer (previously only Postgres +
    # Celery). embedding_client/reranker_client are constructed once here
    # and held for the process lifetime (proposal §Step 4: "load the
    # production model once at startup... a single sentence embeds in
    # ~15ms" — the same SentenceTransformer/CrossEncoder instances are
    # thread-safe for inference, shared across every request).
    vectordb_provider_factory = VectorDBProviderFactory(config=settings, db_client=app.db_client)
    app.vectordb_client = vectordb_provider_factory.create(provider=settings.VECTOR_DB_BACKEND)
    app.chunk_model = await ChunkModel.create_instance(app.db_client, vectordb_client=app.vectordb_client)

    app.embedding_client = LLMProviderFactory(config=settings).create(provider=settings.EMBEDDING_BACKEND)
    app.reranker_client = RerankerProviderFactory(config=settings).create(provider=settings.RERANKER_BACKEND)

    # Both providers lazy-load their actual model weights on first use
    # (BGEM3Provider._get_model / CrossEncoderProvider._get_model), so
    # constructing them above does NOT load anything yet — without this,
    # that one-time weight-loading cost (measured: not fast on this dev
    # machine's hardware) would land inline on whichever real request
    # happens to be first, instead of here at boot where it belongs.
    await app.embedding_client.embed_text(["warmup"], is_query=False)
    await app.reranker_client.rerank("warmup", ["warmup"], top_k=1)

    app.retrieval_controller = RetrievalController(
        client_config_model=app.client_config_model,
        chunk_model=app.chunk_model,
        embedding_client=app.embedding_client,
        reranker_client=app.reranker_client,
    )

    # Section 3 Step 1 — the WhatsApp text pipeline. generation_client is
    # a thin HTTP adapter (no local weights to warm up here, unlike
    # embedding_client/reranker_client above); it is not health-checked
    # at boot so a not-yet-reachable Qwen endpoint never blocks the rest
    # of this already-working API process from starting — Mode A calls
    # simply fail lazily on first use until GENERATION_BASE_URL points at
    # a real server (see README).
    app.generation_client = GenerationProviderFactory(config=settings).create(provider=settings.GENERATION_BACKEND)

    # Dual-model architecture, 2026-08-30: a second, independent client
    # dedicated to IntentRoutingController's classify_intent/rewrite_query
    # calls (split into two focused calls 2026-08-31 — see
    # QueryRouterInterface's own docstring for why) — deliberately NOT the
    # same object as app.generation_client above (see
    # QueryRouterProviderFactory/NileChat12BBaseProvider's own docstrings
    # for the real production evidence behind splitting these). Same lazy-fail
    # rationale as generation_client — not health-checked at boot, a
    # not-yet-reachable sidecar just means both calls fall back to their
    # own safe defaults until QUERY_ROUTER_BASE_URL points at a real
    # server.
    app.query_router_client = QueryRouterProviderFactory(config=settings).create(
        provider=settings.QUERY_ROUTER_BACKEND,
    )

    app.chat_history_model = await ChatHistoryModel.create_instance(app.db_client)
    app.dialogue_state_template_map_model = await DialogueStateTemplateMapModel.create_instance(app.db_client)
    app.session_store = SessionStore(
        redis_url=settings.SESSION_REDIS_URL,
        ttl_seconds=settings.SESSION_TTL_SECONDS,
        history_window=settings.SESSION_HISTORY_WINDOW,
    )

    # Post-fine-tuning safety gate (Implementation Plan's Step 8, following
    # Step 7's LoRA fine-tune) — verifies every Mode A reply's phrasing
    # against its own extracted debug_json before TextReplyController
    # returns it, escalating a rejection to human_handoff_queue instead of
    # showing the patient an unverified numeric claim.
    app.human_handoff_queue_model = await HumanHandoffQueueModel.create_instance(app.db_client)
    app.reply_verification_controller = ReplyVerificationController(
        human_handoff_queue_model=app.human_handoff_queue_model,
    )

    app.text_reply_controller = TextReplyController(
        retrieval_controller=app.retrieval_controller,
        client_config_model=app.client_config_model,
        generation_client=app.generation_client,
        dialogue_state_template_map_model=app.dialogue_state_template_map_model,
        reply_verification_controller=app.reply_verification_controller,
        history_window=settings.SESSION_HISTORY_WINDOW,
    )
    app.intent_routing_controller = IntentRoutingController(
        query_router_client=app.query_router_client,
        text_reply_controller=app.text_reply_controller,
        chat_history_model=app.chat_history_model,
        session_store=app.session_store,
    )

    # Analytics Dashboard pipeline (2026-09-08) — IntentLogModel itself
    # already existed (Phase 6), but was only ever instantiated inside the
    # Celery worker's own composition root (celery_app.py's
    # get_setup_utils), never attached to this FastAPI app — the write
    # path (tasks/log_intent.py) still owns that instance; this is a
    # separate, read-only instance for the new dashboard endpoint.
    app.intent_log_model = await IntentLogModel.create_instance(app.db_client)
    app.analytics_controller = AnalyticsController(intent_log_model=app.intent_log_model)


async def shutdown_span():
    await app.session_store.close()
    app.db_engine.dispose()


app.on_event("startup")(startup_span)
app.on_event("shutdown")(shutdown_span)

app.include_router(base.base_router)
app.include_router(sync.sync_router)
app.include_router(retrieval.retrieval_router)
app.include_router(whatsapp.whatsapp_router)
app.include_router(analytics.analytics_router)
