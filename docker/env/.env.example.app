# Env file for the fastapi/celery-worker containers specifically — same
# fields as src/.env.example, but pointing at in-network service hostnames
# (pgvector/redis/rabbitmq) instead of localhost/exposed host ports, since
# containers reach each other over the `backend` Docker network directly.
# Copy to docker/env/.env.app (untracked) and fill in real values matching
# whatever you put in .env.postgres / .env.redis / .env.rabbitmq.

APP_NAME="Raylab"
APP_VERSION="0.1"

POSTGRES_USERNAME="postgres"
POSTGRES_PASSWORD=
POSTGRES_HOST="pgvector"
POSTGRES_PORT=5432
POSTGRES_MAIN_DATABASE="raylab"

MSAL_CLIENT_ID=
TOKEN_CACHE_ENCRYPTION_KEY=
ONEDRIVE_AUTH_BACKEND="MSAL_GRAPH"

DEFAULT_BOILERPLATE_THRESHOLD=0.9

CELERY_BROKER_URL="amqp://raylab_user:raylab_rabbitmq_2222@rabbitmq:5672/raylab_vhost"
CELERY_RESULT_BACKEND="redis://:raylab_redis_2222@redis:6379/0"
CELERY_TASK_SERIALIZER="json"
CELERY_TASK_TIME_LIMIT=600
CELERY_TASK_ACKS_LATE=true
CELERY_WORKER_CONCURRENCY=2
