# Raylab

Multi-tenant RAG backend (Section 2: OneDrive Integration). See `claude.md` for the full
architectural constitution and `D:\project\Implementation Plan — Section 2 The Multi-Tenant.md`
for the build order this project follows step by step.

## Status

**Step 4 of 9 complete: On-Demand Sync Trigger.**

Implemented so far:
- **Step 1** — `knowledge_chunks` (pgvector store, `client_id NOT NULL`, `embedding VECTOR(1024)`), `client_config` (tenant registry), `ChunkModel`, `ClientConfigModel`, `BucketEnum`.
- **Step 2** — `helpers/config.py` extended (`EMBEDDING_BACKEND_LITERAL`, `DEFAULT_BOILERPLATE_THRESHOLD`); `schema_registry` table + `SchemaRegistryModel` (auto-discovery/auto-registration, upsert-on-discovery, zero manual DB entry); stateless `utils/dynamic_schema_loader.py`.
- **Step 3** — `stores/onedrive/*` (Interface/Enums/Factory/`MSALGraphProvider`), `TokenCacheModel` (Fernet-encrypted, DB-backed token cache, `client_id`-scoped, never plaintext).
- **Step 4** — `main.py` + `celery_app.py` (the two composition roots), `routes/sync.py` (`POST /api/sync`, `GET /api/sync/{task_id}/status`), `controllers/SyncController.py`, `tasks/onedrive_sync.py`. Redis/RabbitMQ added as Docker services; **no celery-beat service, no `beat_schedule` entry** — sync stays human-initiated only.
- Four chained Alembic migrations: `7ec06e4ca8ba` (Step 1) → `48d3c854b890` (Step 2) → `5ef02a92a86f` (Step 3) → Step 4's `client_config.admin_api_key` column (see Step 4 section below for its revision id).

Not built yet (later steps): dynamic sheet parsing/bucket routing, chunking engine, vector storage layer, embedding shootout, retrieval endpoint. Do not assume any of these exist.

## Prerequisites

- WSL (Ubuntu) terminal
- Docker Desktop with the WSL2 backend enabled
- Python 3.10+ inside WSL
- DBeaver (DB inspection) and Postman (API testing, once endpoints exist) on the Windows host

## Local Setup & Development

All commands below are run from a **WSL terminal**, with the working directory at the
Windows-mounted repo path, e.g. `cd /mnt/d/Raylab_Project`.

### 1. Environment setup (conda)

```bash
conda create -n raylab python=3.11 -y
conda activate raylab
```

Python 3.11 is required — `pgvector` and other pinned dependencies need Python ≥3.9.

### 2. Dependencies

```bash
cd src
pip install -r requirements.txt
```

Installs `SQLAlchemy`, `asyncpg`, `psycopg2-binary`, `alembic`, `pgvector`, `python-dotenv`,
`pydantic-settings` (Steps 1–2 scope — Step 2 needed no new packages; later steps append their
own dependencies here as they're built, e.g. `pandas`/`openpyxl` for Step 5, `msal` for Step 3).

### 3. App environment file

```bash
cp .env.example .env
# defaults already match docker/env/.env.example.postgres — edit only if you changed those
```

### 4. Infrastructure (Docker — PostgreSQL + pgvector)

```bash
cd ../docker
cp env/.env.example.postgres env/.env.postgres
# edit docker/env/.env.postgres if you want a non-default password

docker compose up -d pgvector
docker compose ps            # wait until pgvector is "healthy"
cd ..
```

Postgres is now reachable from the host at `localhost:5433` (mapped from the container's 5432),
database `raylab`.

### 5. Database migrations (Alembic)

`alembic/env.py` is architecturally configured to run `CREATE EXTENSION IF NOT EXISTS vector;`
against the connection at the start of every online migration run (`run_migrations_online()`,
before `context.configure(...)`). This means a fresh Postgres volume never needs a manual
`psql` step to enable pgvector's `vector` type — Alembic prepares it just-in-time on its own,
every time. `env.py` also registers a `render_item` hook so `pgvector.sqlalchemy.Vector`
columns render with a correct, self-contained import in generated migrations.

```bash
cd src/models/db_schemes/raylab
cp alembic.ini.example alembic.ini
# edit alembic.ini's sqlalchemy.url only if you changed the Postgres port/password/db name
```

Generate a migration from the current ORM models (`schemes/knowledge_chunk.py`, `client_config.py`):

```bash
alembic revision --autogenerate -m "create knowledge_chunks and client_config"
```

Apply it:

```bash
alembic upgrade head
alembic current            # should print the new revision id, marked (head)
```

## Verifying Step 1

Inspect the schema with `psql` (or the DBeaver equivalent — connect to `localhost:5433`,
database `raylab`, and open the Columns/Indexes tabs for each table):

```bash
docker exec -it raylab-pgvector psql -U postgres -d raylab -c "SELECT extname FROM pg_extension WHERE extname = 'vector';"
docker exec -it raylab-pgvector psql -U postgres -d raylab -c '\d knowledge_chunks'
docker exec -it raylab-pgvector psql -U postgres -d raylab -c '\d client_config'
```

Confirm: `client_id` is `NOT NULL` on `knowledge_chunks`; `embedding` is `vector(1024)`;
`metadata` is `jsonb`; indexes `idx_chunks_client` (btree), `idx_chunks_metadata` (gin), and
`idx_chunks_embedding_hnsw` (hnsw) are all present.

Functional checks — insert a tenant config row and confirm persistence:

```bash
docker exec -it raylab-pgvector psql -U postgres -d raylab -c \
  "INSERT INTO client_config (client_id) VALUES ('cairoscan');"
docker exec -it raylab-pgvector psql -U postgres -d raylab -c \
  "SELECT * FROM client_config;"
```

Confirm multi-tenant isolation is enforced at the database level, not just in application code —
this insert **must fail**:

```bash
docker exec -it raylab-pgvector psql -U postgres -d raylab -c \
  "INSERT INTO knowledge_chunks (client_id, content) VALUES (NULL, 'test');"
```

Expected: `ERROR: null value in column "client_id" of relation "knowledge_chunks" violates not-null constraint`

## Step 2: Core Configuration & Dynamic Schema Registry

### Architectural additions

- **`helpers/config.py`** — extended additively with MSAL scaffolding (`MSAL_CLIENT_ID`,
  `MSAL_TOKEN_CACHE_PATH`, reserved for Step 3), `EMBEDDING_BACKEND_LITERAL` (Step 8's two
  shootout candidates), and `DEFAULT_BOILERPLATE_THRESHOLD` (a platform-wide fallback only — a
  client's actual threshold always comes from its own `client_config` row). Still zero logic,
  typed fields only.
- **`schema_registry` table + `SchemaRegistryModel`** — one row per `(client_id, sheet_name)`,
  holding the sheet's discovered column list, its `BucketEnum` bucket, and its mandatory fields.
  Rows are written by the pipeline itself via `get_or_register()` (upsert-on-discovery) — never
  hand-typed by an admin. An unregistered sheet is auto-inserted with `bucket='VECTOR_DB'` and no
  mandatory fields; an already-registered sheet has its column list refreshed on every call
  (picking up drift), while its bucket/mandatory fields are left untouched, since reclassifying
  those is a separate, deliberate step (`update_bucket` / `set_mandatory_fields`).
- **`utils/dynamic_schema_loader.py`** — the stateless call site Step 5's Sheet Dispatcher will
  use: forwards whatever columns it's handed straight into `SchemaRegistryModel.get_or_register()`,
  with zero sheet-specific branching. This is what keeps the pipeline schema-agnostic — the same
  function registers a medical branch directory or a real-estate listings sheet identically.

### Database migrations

```bash
cd src/models/db_schemes/raylab
alembic revision --autogenerate -m "create schema_registry"
alembic upgrade head
alembic current            # should show the new revision, chained after Step 1's, marked (head)
```

No manual edits to the generated migration were needed — `schema_registry` uses only `String`,
`ARRAY(String)`, and `DateTime`, all natively rendered by Alembic (unlike Step 1's
`pgvector.Vector`, which needed the `render_item` hook in `env.py`).

### Verifying auto-discovery, column drift, and idempotency

Run from `src/`, with the conda env active:

```bash
cd /mnt/d/Raylab_Project/src
python <<'EOF'
import asyncio
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from helpers.config import get_settings
from models.SchemaRegistryModel import SchemaRegistryModel
from models.enums.BucketEnum import BucketEnum


async def main():
    settings = get_settings()
    conn = (
        f"postgresql+asyncpg://{settings.POSTGRES_USERNAME}:{settings.POSTGRES_PASSWORD}"
        f"@{settings.POSTGRES_HOST}:{settings.POSTGRES_PORT}/{settings.POSTGRES_MAIN_DATABASE}"
    )
    engine = create_async_engine(conn)
    db_client = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    registry = await SchemaRegistryModel.create_instance(db_client)

    # 1. Auto-discovery: unregistered sheet -> auto-inserted, Bucket A, no mandatory fields
    row = await registry.get_or_register(
        client_id="cairoscan",
        sheet_name="Branch Directory",
        discovered_columns=["Account", "Branch Name", "Address", "Working Hours (weekdays)"],
    )
    assert row.bucket == BucketEnum.VECTOR_DB.value
    assert row.mandatory_fields == []

    # 2. Column drift: client adds "WhatsApp Number" -> re-sync updates the column list in place
    row2 = await registry.get_or_register(
        client_id="cairoscan",
        sheet_name="Branch Directory",
        discovered_columns=["Account", "Branch Name", "Address", "Working Hours (weekdays)", "WhatsApp Number"],
    )
    assert "WhatsApp Number" in row2.columns

    # 3. Idempotency: identical call again -> same row, no duplicate
    row3 = await registry.get_or_register(
        client_id="cairoscan",
        sheet_name="Branch Directory",
        discovered_columns=["Account", "Branch Name", "Address", "Working Hours (weekdays)", "WhatsApp Number"],
    )
    assert row3.columns == row2.columns

    all_rows = await registry.list_sheets(client_id="cairoscan")
    assert len(all_rows) == 1   # still exactly one row -> no duplicate was ever created

    await engine.dispose()
    print("ALL CHECKS PASSED")


asyncio.run(main())
EOF
```

Confirm at the DB level too:

```bash
docker exec -it raylab-pgvector psql -U postgres -d raylab -c \
  "SELECT client_id, sheet_name, bucket, columns, mandatory_fields FROM schema_registry;"
```

Expect exactly one row for `('cairoscan', 'Branch Directory')`, `bucket = 'VECTOR_DB'`, `columns`
including `WhatsApp Number`, `mandatory_fields = '{}'`.

## Step 3: OneDrive/MSAL Store — Authentication & Fetch Abstraction

### Architectural additions

- **`stores/onedrive/OneDriveInterface.py` / `OneDriveEnums.py` / `OneDriveProviderFactory.py` / `providers/MSALGraphProvider.py`** —
  the same Ports & Adapters pattern as `stores/llm`/`stores/vectordb`. `authenticate(client_id)` and
  `fetch_file(client_id, item_id)` are the only two contract methods; `client_id` is required, no
  default, on both. `MSALGraphProvider` uses MSAL Device Code Flow against the `/consumers`
  authority (OneDrive Personal only — never Client Credentials Flow), offloads every blocking
  `msal`/`requests` call via `asyncio.to_thread`, and never opens its own DB session — it only
  calls the injected `TokenCacheModel`.
- **`TokenCacheModel`** — each client's MSAL token cache, Fernet-encrypted at rest
  (`TOKEN_CACHE_ENCRYPTION_KEY`), keyed strictly by `client_id`. `authenticate()` raises
  `ClientNotOnboardedError` — never a silent fallback to another tenant's cache — if no cache
  row exists yet, or if silent token refresh fails (revoked/expired refresh token).

### Database migrations

```bash
cd src/models/db_schemes/raylab
alembic revision --autogenerate -m "create token_cache"
alembic upgrade head
alembic current            # should show the new revision, chained after Step 2's, marked (head)
```

### Verifying the "not onboarded" isolation error (no Azure setup needed)

```bash
cd /mnt/d/Raylab_Project/src
python <<'EOF'
import asyncio
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from helpers.config import get_settings
from models.TokenCacheModel import TokenCacheModel
from stores.onedrive.OneDriveProviderFactory import OneDriveProviderFactory
from stores.onedrive.OneDriveInterface import ClientNotOnboardedError


async def main():
    settings = get_settings()
    conn = (
        f"postgresql+asyncpg://{settings.POSTGRES_USERNAME}:{settings.POSTGRES_PASSWORD}"
        f"@{settings.POSTGRES_HOST}:{settings.POSTGRES_PORT}/{settings.POSTGRES_MAIN_DATABASE}"
    )
    engine = create_async_engine(conn)
    db_client = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    token_cache_model = await TokenCacheModel.create_instance(db_client)
    factory = OneDriveProviderFactory(config=settings, token_cache_model=token_cache_model)
    provider = factory.create(provider=settings.ONEDRIVE_AUTH_BACKEND)

    try:
        await provider.fetch_file(client_id="not-a-real-client", item_id="whatever")
        print("FAIL: expected ClientNotOnboardedError")
    except ClientNotOnboardedError as e:
        print(f"PASS — {e}")

    await engine.dispose()


asyncio.run(main())
EOF
```

The real `fetch_file` success path additionally requires a one-time Azure AD public-client app
registration (Personal Microsoft accounts only, "Allow public client flows" = Yes,
`Files.Read` delegated permission) and an interactive Device Code Flow login via
`provider.register_client_via_device_flow(client_id)` — see the assistant's Step 3 implementation
notes for the full walkthrough if you need to re-run it.

## Step 4: On-Demand Sync Trigger

### Architectural additions

- **`main.py`** — the API-process composition root: builds the Postgres engine, `ClientConfigModel`,
  and `SyncController`, and includes `routes/base.py` + `routes/sync.py`. Deliberately does **not**
  build `TokenCacheModel`/OneDrive — the API process only ever needs Postgres and a Celery client
  to enqueue tasks; OneDrive/MSAL is entirely a worker-process concern.
- **`celery_app.py`** — the worker-process composition root (`get_setup_utils()`, mirroring
  `main.py`'s `startup_span()`). Registers `tasks.onedrive_sync` and its queue in `task_routes`.
  **There is no `beat_schedule` key anywhere in this file** — its absence is the proof that no
  periodic/scheduled trigger exists for OneDrive sync (claude.md §2.3).
- **`routes/sync.py`** — `POST /api/sync` (202 + `task_id`) and `GET /api/sync/{task_id}/status`.
  Pure transport: no DB session or Celery internals touched directly in the route handlers — both
  delegate to `SyncController`.
- **`controllers/SyncController.py`** — resolves `client_id` from an admin API key (see below),
  confirms `client_config.onedrive_item_id` is set, and enqueues `tasks.onedrive_sync.fetch_and_dispatch`.
- **`tasks/onedrive_sync.py`** — the Celery task that actually calls `onedrive_client.fetch_file()`.
  Chained into Step 5's parsing task once it exists; for now it ends at "fetch succeeded."

**On "authenticated admin session":** Section 2's scope doesn't include a full admin-auth system
(no user table, login, or JWT is defined anywhere in the 9 steps). Rather than invent one, `POST
/api/sync` requires an `X-Admin-Api-Key` header, resolved server-side against a new
`client_config.admin_api_key` column via `ClientConfigModel.get_client_id_by_admin_api_key()`.
`client_id` is still never a client-supplied field — it's always resolved from the key, never
trusted from the request body/path/query. This is a deliberate, minimal stand-in; a real
admin-auth system would replace it wholesale, not extend it.

### Infrastructure (Docker — Redis + RabbitMQ)

```bash
cd /mnt/d/Raylab_Project/docker
cp env/.env.example.redis env/.env.redis
cp env/.env.example.rabbitmq env/.env.rabbitmq

docker compose up -d redis rabbitmq
docker compose ps            # wait until both are "healthy"
cd ..
```

Redis is reachable at `localhost:6380`, RabbitMQ's AMQP port at `localhost:5673` (management UI
at `localhost:15673`).

### App environment file

Add to `src/.env` (values must match whatever you set in `docker/env/.env.redis` /
`.env.rabbitmq`):
```
CELERY_BROKER_URL="amqp://raylab_user:raylab_rabbitmq_2222@localhost:5673/raylab_vhost"
CELERY_RESULT_BACKEND="redis://:raylab_redis_2222@localhost:6380/0"
CELERY_TASK_SERIALIZER="json"
CELERY_TASK_TIME_LIMIT=600
CELERY_TASK_ACKS_LATE=true
CELERY_WORKER_CONCURRENCY=2
```

### Database migration — `client_config.admin_api_key`

```bash
cd src/models/db_schemes/raylab
alembic revision --autogenerate -m "add admin_api_key to client_config"
alembic upgrade head
alembic current
```

### Seed a test admin API key

```bash
docker exec -it raylab-pgvector psql -U postgres -d raylab -c \
  "UPDATE client_config SET admin_api_key = 'test-cairoscan-admin-key', onedrive_item_id = 'placeholder-item-id' WHERE client_id = 'cairoscan';"
```
(If the `cairoscan` row doesn't exist yet, `INSERT INTO client_config (client_id, admin_api_key, onedrive_item_id) VALUES ('cairoscan', 'test-cairoscan-admin-key', 'placeholder-item-id');` instead.)

### Run the API and worker

Two options — pick one. **Direct (fast iteration, WSL):**

```bash
cd /mnt/d/Raylab_Project/src
uvicorn main:app --reload --port 8000
```
```bash
# separate terminal, same conda env
cd /mnt/d/Raylab_Project/src
celery -A celery_app worker --queues=default,onedrive_sync --loglevel=info
```

**Or fully containerized:**
```bash
cd /mnt/d/Raylab_Project/docker
docker compose up -d --build fastapi celery-worker
```

### Verify

```bash
curl -i -X POST http://localhost:8000/api/sync -H "X-Admin-Api-Key: test-cairoscan-admin-key"
```
Expect `202 Accepted` and a JSON body with a `task_id`. Since `onedrive_item_id` is a placeholder,
the task itself will fail once it actually tries to fetch — that's expected without a real Azure
app/Item ID (see Step 3's optional real-fetch path). Check status:
```bash
curl http://localhost:8000/api/sync/<task_id>/status -H "X-Admin-Api-Key: test-cairoscan-admin-key"
```
Watch `status` move `PENDING` → `STARTED` → `FAILURE` (or `SUCCESS` if you completed Step 3's
real device-flow onboarding and used a real Item ID).

Confirm the invalid-key path is rejected:
```bash
curl -i -X POST http://localhost:8000/api/sync -H "X-Admin-Api-Key: not-a-real-key"
```
Expect `401`.

Confirm no periodic entry exists for this task — this is the proof polling was never
reintroduced:
```bash
celery -A celery_app inspect scheduled
```
Expect an empty result for every worker (no scheduled entries at all).

## Environment variables

| Variable | File | Purpose |
|---|---|---|
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | `docker/env/.env.postgres` | Postgres container credentials |
| `REDIS_PASSWORD` | `docker/env/.env.redis` | Redis container credential |
| `RABBITMQ_DEFAULT_USER` / `_PASS` / `_VHOST` | `docker/env/.env.rabbitmq` | RabbitMQ container credentials |
| `APP_NAME`, `APP_VERSION` | `src/.env` | App identity (used by `helpers/config.py`) |
| `POSTGRES_USERNAME` / `POSTGRES_PASSWORD` / `POSTGRES_HOST` / `POSTGRES_PORT` / `POSTGRES_MAIN_DATABASE` | `src/.env` | App-side Postgres connection (host: `localhost`, port: `5433`) |
| `MSAL_CLIENT_ID` | `src/.env` | Azure AD public-client app ID (required — see Step 3) |
| `TOKEN_CACHE_ENCRYPTION_KEY` | `src/.env` | Fernet key encrypting `token_cache.encrypted_cache` (required — see Step 3) |
| `ONEDRIVE_AUTH_BACKEND` / `ONEDRIVE_AUTH_BACKEND_LITERAL` | `src/.env` | OneDrive provider selection (currently only `MSAL_GRAPH`) |
| `EMBEDDING_BACKEND_LITERAL` | `src/.env` | Self-documenting list of implemented embedding backends (Step 8 selects one) |
| `DEFAULT_BOILERPLATE_THRESHOLD` | `src/.env` | Platform-wide fallback only — per-client value always comes from `client_config.boilerplate_threshold` |
| `CELERY_BROKER_URL` / `CELERY_RESULT_BACKEND` / `CELERY_TASK_*` | `src/.env` | Celery task queue config (Step 4) |

## Project layout

See `claude.md` §1.1 for the full target directory tree. Only the Steps 1–4 subset exists today:

```
Raylab_Project/
├── docker/
│   ├── docker-compose.yml      # pgvector, redis, rabbitmq, fastapi, celery-worker
│   │                           #   (no celery-beat, deliberately)
│   ├── env/.env.example.{postgres,redis,rabbitmq,app}
│   └── raylab/{Dockerfile, entrypoint.sh, alembic.example.ini}
├── src/
│   ├── main.py                 # API-process composition root
│   ├── celery_app.py           # worker-process composition root, no beat_schedule
│   ├── requirements.txt
│   ├── .env.example
│   ├── helpers/config.py       # Postgres + MSAL/OneDrive + Celery + embedding/threshold literals
│   ├── routes/
│   │   ├── base.py
│   │   ├── sync.py
│   │   └── schemes/sync.py
│   ├── controllers/
│   │   ├── BaseController.py
│   │   └── SyncController.py
│   ├── tasks/
│   │   └── onedrive_sync.py
│   ├── stores/onedrive/
│   │   ├── OneDriveInterface.py, OneDriveEnums.py, OneDriveProviderFactory.py
│   │   └── providers/MSALGraphProvider.py
│   ├── utils/
│   │   └── dynamic_schema_loader.py   # stateless auto-discovery call site (Step 5 wires it up)
│   └── models/
│       ├── BaseDataModel.py
│       ├── ChunkModel.py
│       ├── ClientConfigModel.py       # + get_client_id_by_admin_api_key
│       ├── SchemaRegistryModel.py     # get_or_register / update_bucket / set_mandatory_fields
│       ├── TokenCacheModel.py         # Fernet-encrypted, client_id-scoped
│       ├── enums/BucketEnum.py
│       └── db_schemes/raylab/
│           ├── schemes/{raylab_base,knowledge_chunk,client_config,schema_registry,token_cache}.py
│           ├── alembic.ini.example
│           └── alembic/{env.py, script.py.mako, versions/}
├── .gitignore
├── claude.md
└── README.md
```
