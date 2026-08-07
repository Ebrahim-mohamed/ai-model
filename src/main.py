from fastapi import FastAPI
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from routes import base, sync, retrieval
from helpers.config import get_settings
from models.ClientConfigModel import ClientConfigModel
from models.ChunkModel import ChunkModel
from controllers.SyncController import SyncController
from controllers.RetrievalController import RetrievalController
from stores.vectordb.VectorDBProviderFactory import VectorDBProviderFactory
from stores.llm.LLMProviderFactory import LLMProviderFactory
from stores.reranker.RerankerProviderFactory import RerankerProviderFactory

app = FastAPI()


async def startup_span():
    settings = get_settings()

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


async def shutdown_span():
    app.db_engine.dispose()


app.on_event("startup")(startup_span)
app.on_event("shutdown")(shutdown_span)

app.include_router(base.base_router)
app.include_router(sync.sync_router)
app.include_router(retrieval.retrieval_router)
