from celery import Celery

from helpers.config import get_settings
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from models.ClientConfigModel import ClientConfigModel
from models.TokenCacheModel import TokenCacheModel
from stores.onedrive.OneDriveProviderFactory import OneDriveProviderFactory

settings = get_settings()


async def get_setup_utils():
    """The worker-process composition root — mirrors main.py's
    startup_span() for the API process. These two functions are the only
    places concrete provider classes get instantiated (claude.md §1.2)."""
    settings = get_settings()

    postgres_conn = (
        f"postgresql+asyncpg://{settings.POSTGRES_USERNAME}:{settings.POSTGRES_PASSWORD}"
        f"@{settings.POSTGRES_HOST}:{settings.POSTGRES_PORT}/{settings.POSTGRES_MAIN_DATABASE}"
    )
    db_engine = create_async_engine(postgres_conn)
    db_client = sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

    client_config_model = await ClientConfigModel.create_instance(db_client)
    token_cache_model = await TokenCacheModel.create_instance(db_client)

    onedrive_provider_factory = OneDriveProviderFactory(config=settings, token_cache_model=token_cache_model)
    onedrive_client = onedrive_provider_factory.create(provider=settings.ONEDRIVE_AUTH_BACKEND)

    return db_engine, client_config_model, token_cache_model, onedrive_client


celery_app = Celery(
    "raylab",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=[
        "tasks.onedrive_sync",
    ],
)

celery_app.conf.update(
    task_serializer=settings.CELERY_TASK_SERIALIZER,
    result_serializer=settings.CELERY_TASK_SERIALIZER,
    accept_content=[settings.CELERY_TASK_SERIALIZER],

    # Task safety - late acknowledgment prevents task loss on worker crash
    task_acks_late=settings.CELERY_TASK_ACKS_LATE,

    # Time limits - prevent hanging tasks
    task_time_limit=settings.CELERY_TASK_TIME_LIMIT,

    # Result backend - store results for status tracking
    task_ignore_result=False,
    result_expires=3600,

    worker_concurrency=settings.CELERY_WORKER_CONCURRENCY,

    broker_connection_retry_on_startup=True,
    broker_connection_retry=True,
    broker_connection_max_retries=10,
    worker_cancel_long_running_tasks_on_connection_loss=True,

    task_routes={
        "tasks.onedrive_sync.fetch_and_dispatch": {"queue": "onedrive_sync"},
    },

    timezone="UTC",

    # No beat_schedule — deliberately absent, not an oversight. OneDrive is
    # Personal (claude.md §2.3): sync is human-initiated only, via
    # POST /api/sync. A periodic entry here would reintroduce the polling
    # this architecture explicitly rejects. Its absence IS the verification.
)

celery_app.conf.task_default_queue = "default"
