from fastapi import FastAPI
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from routes import base, sync
from helpers.config import get_settings
from models.ClientConfigModel import ClientConfigModel
from controllers.SyncController import SyncController

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

    # The API process only ever needs Postgres + a Celery client to enqueue
    # tasks — OneDrive/MSAL is a worker-process concern (celery_app.py's own
    # composition root), never instantiated here.
    app.client_config_model = await ClientConfigModel.create_instance(app.db_client)
    app.sync_controller = SyncController(client_config_model=app.client_config_model)


async def shutdown_span():
    app.db_engine.dispose()


app.on_event("startup")(startup_span)
app.on_event("shutdown")(shutdown_span)

app.include_router(base.base_router)
app.include_router(sync.sync_router)
