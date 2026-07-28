# Raylab

Multi-tenant RAG backend (Section 2: OneDrive Integration). See `claude.md` for the full
architectural constitution and `D:\project\Implementation Plan — Section 2 The Multi-Tenant.md`
for the build order this project follows step by step.

## Status

**Step 9 of 9 complete: Hybrid Retrieval Endpoint.** All nine Implementation Plan steps are now
built. Real end-to-end verification of the retrieval endpoint's dense-vector leg is still pending
a full production embedding backfill finishing on this dev machine's hardware (see the Step 9
section below) — BM25/keyword retrieval, RRF fusion, re-ranking, metadata-filter validation, and
`client_id` isolation are all already verified against real data.

Implemented so far:
- **Step 1** — `knowledge_chunks` (pgvector store, `client_id NOT NULL`, `embedding VECTOR(1024)`), `client_config` (tenant registry), `ChunkModel`, `ClientConfigModel`, `BucketEnum`.
- **Step 2** — `helpers/config.py` extended (`EMBEDDING_BACKEND_LITERAL`); `schema_registry` table + `SchemaRegistryModel` (auto-discovery/auto-registration, upsert-on-discovery, zero manual DB entry); stateless `utils/dynamic_schema_loader.py`.
- **Step 3** — `stores/onedrive/*` (Interface/Enums/Factory/`MSALGraphProvider`), `TokenCacheModel` (Fernet-encrypted, DB-backed token cache, `client_id`-scoped, never plaintext).
- **Step 4** — `main.py` + `celery_app.py` (the two composition roots), `routes/sync.py` (`POST /api/sync`, `GET /api/sync/{task_id}/status`), `controllers/SyncController.py`, `tasks/onedrive_sync.py`. Redis/RabbitMQ added as Docker services; **no celery-beat service, no `beat_schedule` entry** — sync stays human-initiated only.
- **Step 5** — `controllers/DocumentParsingController.py` (auto-discovers/auto-registers sheets, routes rows by `BucketEnum`, stamps every row with its `sheet_name`); `tasks/document_parsing.py` (chained after `fetch_and_dispatch`); `staging_rows` table + `StagingRowModel` for **Bucket A only**. Bucket B/C are never written to the database — `utils/template_file_writer.py` renders them into generated `stores/llm/templates/clients/<client_id>/{prompt_templates,system_directives}.py` modules instead (see the Step 5 section below).
- **Step 6** — `controllers/ChunkingController.py` (Bucket A only): a two-step, per-row process — concatenate every non-empty field into `"label: value"` in schema order, then prepend `[Document: {file_name}] ` to the finished string. `tasks/chunk_generation.py` (`generate_chunks`, chained after `parse_and_stage`) writes the result into `knowledge_chunks` via `ChunkModel`, delete-and-reinsert scoped to `(client_id, source_file)`. See the Step 6 section below, including why an earlier statistical boilerplate-exclusion mechanism was built and then fully removed.
- **Step 7** — `stores/vectordb/*` (`VectorDBInterface`/`VectorDBEnums`/`VectorDBProviderFactory`/`providers/PGVectorProvider.py`): the Ports & Adapters abstraction over Postgres/pgvector, `client_id` required on every method. `ChunkModel.insert_many_chunks`/`search_by_vector` now delegate to this adapter instead of running their own SQL. No schema change — no new migration for this step.
- **Step 8** — `stores/llm/*` (`LLMInterface`/`LLMEnums`/`LLMProviderFactory`/`providers/{BGEM3Provider,SwanLargeProvider}.py`); `controllers/EmbeddingShootoutController.py` (orchestrates the benchmark, no embedding math of its own); `models/EvaluationQueryModel.py` + `evaluation_queries`/`shootout_results` tables; `tasks/embedding_shootout.py`. **BGE-M3 is fully real and working**; **Swan-Large is deferred** — its HuggingFace repo is gated (401 on both the model page and API) and it's built on a 7B-parameter backbone this dev machine's hardware can't run. See the Step 8 section below for the full research and why the architecture is complete regardless.
- **Production embedding generation** (bridges Step 8 into actual use) — `controllers/EmbeddingGenerationController.py`, `tasks/embedding_generation.py` (`generate_embeddings`), chained automatically after every `generate_chunks` run, plus reusable standalone as a backfill for chunks synced before an embedding backend was promoted. `stores/vectordb`'s `VectorDBInterface`/`PGVectorProvider` gained `update_embeddings`; `ChunkModel` gained `update_embeddings` (delegating) + `get_chunks_without_embedding`. Writes commit incrementally per batch, never one all-or-nothing write. See the note in the Step 9 section below.
- **Step 9** — `routes/retrieval.py` (`POST /api/retrieve`), `routes/schemes/retrieval.py`, `controllers/RetrievalController.py` (validates `metadata_filters` against `client_config.allowed_metadata_keys`, embeds the query, calls hybrid search, then re-ranks), `stores/reranker/*` (`RerankerInterface`/`RerankerEnums`/`RerankerProviderFactory`/`providers/CrossEncoderProvider.py`). `PGVectorProvider` gained `search_by_bm25` and `hybrid_search` (dense + sparse + Reciprocal Rank Fusion). `client_config` gained `retrieval_top_k`/`rrf_k` — never hardcoded (claude.md §1.3). See the Step 9 section below.
- Nine chained Alembic migrations: `7ec06e4ca8ba` (Step 1) → `48d3c854b890` (Step 2) → `5ef02a92a86f` (Step 3) → `5ab64e68194b` (`client_config.admin_api_key`) → `0e243cbc913b` (`staging_rows`) → `a0db1131db2f` (`client_config.onedrive_drive_id`) → `ff0b5276e5cb` (Step 6's drop of `client_config.boilerplate_threshold`) → `83b89604b9c4` (Step 8's `evaluation_queries` + `shootout_results`) → `32f61443e199` (Step 9's `client_config.retrieval_top_k`/`rrf_k`). Step 7 and the embedding-generation addition added no migration.

Not built (out of Section 2's scope entirely): `TemplateParser`'s actual runtime consumer — the
WhatsApp/Voice workflows (Sections 3/4) that would call it — since those are explicitly out of
scope (claude.md §2.1). Everything in the nine Implementation Plan steps for Section 2 exists.

**Note on `client_id` in this environment:** the step-by-step walkthroughs below (Steps 1–5)
were written and tested using `client_id='cairoscan'` as the illustrative example. The real,
currently-onboarded client in this actual running environment is `client_id='raylab'`
(`admin_api_key='raylab-admin-test-key'`) — see **Daily Startup** below. When following the
historical `psql`/script examples further down, substitute `raylab` for `cairoscan` unless you've
onboarded `cairoscan` for real too.

## Prerequisites

- WSL (Ubuntu) terminal
- Docker Desktop with the WSL2 backend enabled
- Python 3.10+ inside WSL
- DBeaver (DB inspection) and Postman (API testing, once endpoints exist) on the Windows host
## Daily Startup (Resuming Work)

Once everything from Steps 1–5 is already built and migrated, this is everything needed to bring
the whole stack back up after a reboot/shutdown — no setup, just starting what already exists.
Skip to **Local Setup & Development** below only if you're setting this up for the first time.

### 1. Start Docker Desktop

Open Docker Desktop on Windows and wait until it's fully running (whale icon settled in the system
tray) before continuing — WSL2's Docker integration isn't ready until it is.

### 2. Start the infrastructure containers

```bash
cd /mnt/d/Raylab_Project/docker
docker compose up -d pgvector redis rabbitmq
docker compose ps
```

Wait until all three show `healthy` — don't move on if any is still `starting`. If a container
shows unhealthy or won't start, see the troubleshooting note at the end of this section.

### 3. Activate the Python environment

```bash
conda activate raylab
```

### 4. Start the Celery worker (its own terminal — leave it running)

```bash
cd /mnt/d/Raylab_Project/src
celery -A celery_app worker --queues=default,onedrive_sync,document_parsing --loglevel=info
```

Confirm the startup banner lists both `tasks.onedrive_sync.fetch_and_dispatch` and
`tasks.document_parsing.parse_and_stage` under `[tasks]`, and ends with `celery@<host> ready.`

### 5. Start the FastAPI server (a second terminal — leave it running too)

```bash
cd /mnt/d/Raylab_Project/src
uvicorn main:app --reload --port 8000
```

### 6. Smoke-test before touching Postman

```bash
curl http://localhost:8000/api/
```
Expect `{"app_name":"Raylab","app_version":"0.1"}`. If this hangs or errors, the API can't reach
Postgres — re-check step 2's container health before going further.

### 7. Resume testing in Postman

Same environment/requests as before — `POST {{base_url}}/api/sync` with header
`X-Admin-Api-Key: raylab-admin-test-key` (the real, currently-onboarded `client_id` this resolves
to is `raylab`, not `cairoscan` — see the Step 4 section below for how that mapping works).

### If you hit a "Connection reset by peer" error on the first request back

This has happened twice in this project already (once against Postgres, once against Redis) —
both times the actual container was fine, but a pooled connection inside a long-lived process
(or, once, the container itself) went stale across a Windows sleep/wake cycle. If it recurs:
```bash
docker restart raylab-pgvector raylab-redis raylab-rabbitmq
```
then repeat step 4–5 (restart the worker and API so they open fresh connections) before retrying
in Postman. `pool_pre_ping=True` on the Postgres engines should self-heal most cases automatically
now; a full restart is the fallback if it doesn't.

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
  `MSAL_TOKEN_CACHE_PATH`, reserved for Step 3) and `EMBEDDING_BACKEND_LITERAL` (Step 8's two
  shootout candidates). Still zero logic, typed fields only.
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

`docker/rabbitmq/rabbitmq.conf` (mounted read-only into the container) sets
`consumer_timeout = 43200000` (12h) — RabbitMQ's default (30 min) closes the channel with a
`PreconditionFailed (406)` if a long-running task (Step 8/9's CPU-only embedding generation,
observed exceeding 30 minutes per file on this dev machine's hardware) doesn't ack in time, since
`task_acks_late=True` means the ack only happens when the task *finishes*. The service also pins
`hostname: raylab-rabbitmq` — without it, RabbitMQ's Mnesia queue/message data is keyed by
Docker's randomized per-container hostname, so any container recreation (e.g. to pick up a config
change) silently loses visibility into the previous node's durable queues, even though the data is
still on the same persisted volume. Both were found and fixed the hard way — a real
`consumer_timeout` crash mid-embedding, then a real lost-queue incident from recreating the
container without a pinned hostname.

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

### Update: shared-folder, multi-file sync (post–Step 5)

`fetch_and_dispatch` no longer fetches a single known file by Item ID. It now lists a shared
OneDrive **folder**'s children via `GET /drives/{driveId}/items/{folderId}/children`, filters to
items that are actual files (not sub-folders) whose name ends in `.xlsx`, and dispatches **one
independent `parse_and_stage` call per file** — each scoped by its own file name as `source_file`,
so a sync only ever replaces that specific file's previously-staged rows, never all of a client's
files at once because one changed.

- **`client_config`** gained `onedrive_drive_id` alongside the existing `onedrive_item_id` —
  `onedrive_item_id` now means the shared folder's own Item ID (not a single workbook's).
  Both are per-client, DB-sourced — never hardcoded in `stores/onedrive`.
- **`OneDriveInterface`** gained a new port method, `fetch_files_in_folder(client_id, drive_id,
  folder_id)` — an async generator yielding `(file_name, file_bytes)` for every `.xlsx` found,
  following Graph's `@odata.nextLink` pagination. `fetch_file` (single-item fetch) is unchanged
  and still used elsewhere (e.g. Item-ID lookups).
- **`fetch_and_dispatch`**'s result shape changed: `parse_task_id` (singular) is now
  `parse_task_ids` + `files_dispatched` (plural), since one sync can now produce many parse tasks.

**Migration:**
```bash
cd src/models/db_schemes/raylab
alembic revision --autogenerate -m "add onedrive_drive_id to client_config"
alembic upgrade head
```

**Re-seeding `client_config` for the shared-folder model** (replaces the single-file `UPDATE` from
the Step 4 section above — `onedrive_item_id` must now be the *folder's* Item ID, not a file's):
```bash
docker exec -it raylab-pgvector psql -U postgres -d raylab -c \
  "UPDATE client_config SET onedrive_drive_id = '<the real driveId>', onedrive_item_id = '<the real folder Item ID>' WHERE client_id = 'cairoscan';"
```

## Step 5: Dynamic 1NF Sheet Parsing & Bucket Routing

### Architectural additions

- **`controllers/DocumentParsingController.py`** — one generic function for every sheet, every
  client. For each sheet in the fetched workbook it calls
  `SchemaRegistryModel.get_or_register(...)` (auto-registering brand-new sheets on the spot,
  defaulted to Bucket A), validates against whichever mandatory fields are already configured,
  and yields every row tagged with its `BucketEnum` — **every row of every bucket also carries
  the `sheet_name` it came from**, injected dynamically from the same `wb.sheetnames` loop, never
  a per-sheet special case (claude.md §3.6). Structural shape (merged cells, multi-row headers) is
  trusted, never inferred, per claude.md §3.1 — a malformed sheet just produces garbage pandas
  columns, which is the correct failure mode, not a bug to detect and repair.
- **`tasks/document_parsing.py`** (`parse_and_stage`) — chained directly off Step 4's
  `fetch_and_dispatch` (which now base64-encodes the fetched bytes and enqueues this task with
  `source_file=onedrive_item_id`). Nothing is written until the *entire* workbook parses
  successfully — one sheet failing mandatory-field validation fails the whole sync, never a
  partial write.
- **`staging_rows` + `StagingRowModel`** — **Bucket A only.** Delete-and-reinsert scoped to
  `client_id` + `source_file`, exactly like `knowledge_chunks`.
- **Bucket B/C are never written to the database** (claude.md §3.5) — `utils/template_file_writer.py`
  is the only code allowed to render them, into `stores/llm/templates/clients/<client_id>/
  {prompt_templates,system_directives}.py`, following the exact `string.Template`-per-variable
  structure `mini-rag-tut-017`'s `templates/locales/<lang>/*.py` already uses. Each row's variable
  name (`template_id`) is derived mechanically — a slug of `sheet_name` plus a slug of the row's
  own first populated field — never a hand-authored mapping. Every sync fully overwrites the
  file (the file-based equivalent of delete-and-reinsert). These generated files are gitignored;
  `TemplateParser` (Step 9) will read them back at request time.
- **`celery_app.py`'s `get_setup_utils()` now returns a dict**, not a positional tuple — additive
  as more models get added across later steps, and `tasks/onedrive_sync.py` was updated to match.

### New dependency

```bash
conda activate raylab
cd /mnt/d/Raylab_Project/src
pip install -r requirements.txt   # adds pandas, openpyxl
```

### Database migration — `staging_rows`

```bash
cd src/models/db_schemes/raylab
alembic revision --autogenerate -m "create staging_rows"
alembic upgrade head
alembic current
```

### Verifying — no live OneDrive needed

Bucket routing, auto-discovery, and the generated template files can all be exercised directly
against a local test workbook, bypassing Step 3's OneDrive fetch entirely. Run from `src/`, conda
env active:

```bash
cd /mnt/d/Raylab_Project/src
python <<'EOF'
import asyncio
import io

import pandas as pd
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from helpers.config import get_settings
from models.SchemaRegistryModel import SchemaRegistryModel
from models.StagingRowModel import StagingRowModel
from models.enums.BucketEnum import BucketEnum
from controllers.DocumentParsingController import DocumentParsingController
from utils.template_file_writer import write_template_file


async def main():
    settings = get_settings()
    conn = (
        f"postgresql+asyncpg://{settings.POSTGRES_USERNAME}:{settings.POSTGRES_PASSWORD}"
        f"@{settings.POSTGRES_HOST}:{settings.POSTGRES_PORT}/{settings.POSTGRES_MAIN_DATABASE}"
    )
    engine = create_async_engine(conn)
    db_client = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    schema_registry_model = await SchemaRegistryModel.create_instance(db_client)
    staging_row_model = await StagingRowModel.create_instance(db_client)

    client_id = "cairoscan"

    # Build a tiny in-memory workbook: one brand-new Bucket-A sheet, one
    # sheet we pre-classify as Bucket C (mimicking an engineer's deliberate
    # reclassification per claude.md §3.2 point 5 — auto-discovery alone
    # would default it to Bucket A).
    branch_directory = pd.DataFrame([
        {"Account": "cairoscan", "Branch Name": "Mohandessen", "Address": "45 Anas Ibn Malek"},
        {"Account": "cairoscan", "Branch Name": "Maadi", "Address": "12 Road 9"},
    ])
    reservation_process = pd.DataFrame([
        {"Step": "1", "Instruction": "Ask for patient name and phone number"},
        {"Step": "2", "Instruction": "Confirm branch and preferred time slot"},
    ])

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        branch_directory.to_excel(writer, sheet_name="Branch Directory", index=False)
        reservation_process.to_excel(writer, sheet_name="Reservation Process", index=False)
    workbook_bytes = buf.getvalue()

    # Pre-classify Reservation Process as Bucket C, as an engineer would
    await schema_registry_model.get_or_register(
        client_id=client_id, sheet_name="Reservation Process",
        discovered_columns=["Step", "Instruction"],
    )
    await schema_registry_model.update_bucket(
        client_id=client_id, sheet_name="Reservation Process",
        bucket=BucketEnum.SYSTEM_DIRECTIVE,
    )

    parser = DocumentParsingController(schema_registry_model=schema_registry_model)

    bucket_a_rows, bucket_c_rows = [], []
    async for bucket, sheet_name, row_data in parser.parse_workbook(client_id=client_id, workbook_bytes=workbook_bytes):
        if bucket == BucketEnum.VECTOR_DB:
            bucket_a_rows.append({"sheet_name": sheet_name, "row_data": row_data})
        elif bucket == BucketEnum.SYSTEM_DIRECTIVE:
            bucket_c_rows.append(row_data)

    print("1) Bucket A rows parsed:", len(bucket_a_rows))
    assert len(bucket_a_rows) == 2
    assert bucket_a_rows[0]["row_data"]["sheet_name"] == "Branch Directory"

    branch_row = await schema_registry_model.get_schema(client_id, "Branch Directory")
    print("2) auto-registered Branch Directory bucket:", branch_row.bucket)
    assert branch_row.bucket == BucketEnum.VECTOR_DB.value

    await staging_row_model.delete_rows_by_source_file(client_id=client_id, source_file="test-source")
    inserted = await staging_row_model.insert_many_rows(client_id=client_id, rows=bucket_a_rows, source_file="test-source")
    print("3) staged rows:", inserted)

    file_path = write_template_file(client_id=client_id, bucket=BucketEnum.SYSTEM_DIRECTIVE, rows=bucket_c_rows)
    print("4) wrote template file:", file_path)
    with open(file_path, encoding="utf-8") as f:
        contents = f.read()
    assert "Template(" in contents
    assert "reservation_process" in contents.lower() or "Step" in contents

    await engine.dispose()
    print("ALL CHECKS PASSED")


asyncio.run(main())
EOF
```

Then confirm at the DB and filesystem level:

```bash
docker exec -it raylab-pgvector psql -U postgres -d raylab -c \
  "SELECT client_id, sheet_name, bucket, row_data->>'sheet_name' AS stamped_sheet FROM staging_rows;"
docker exec -it raylab-pgvector psql -U postgres -d raylab -c \
  "SELECT client_id, sheet_name, bucket FROM schema_registry WHERE client_id = 'cairoscan';"
cat "src/stores/llm/templates/clients/cairoscan/system_directives.py"
```

Expect: two `staging_rows` for `Branch Directory` with `bucket='VECTOR_DB'` and `stamped_sheet='Branch Directory'`; `schema_registry` shows `Branch Directory` auto-registered as `VECTOR_DB` and `Reservation Process` as `SYSTEM_DIRECTIVE`; the generated file contains one `Template(...)` per Reservation Process row, and confirm **no** `prompt_templates`/`system_directives` table exists in Postgres:
```bash
docker exec -it raylab-pgvector psql -U postgres -d raylab -c "\dt"
```

### Verifying through the real sync pipeline (optional, end-to-end)

With the API/worker running (§Step 4) and a real or placeholder OneDrive setup, `POST /api/sync`
now chains automatically into `parse_and_stage` — check its status the same way as `fetch_and_dispatch`:
```bash
curl -X POST http://localhost:8000/api/sync -H "X-Admin-Api-Key: test-cairoscan-admin-key"
# take the parse_task_id from fetch_and_dispatch's result once it's SUCCESS, then:
curl http://localhost:8000/api/sync/<parse_task_id>/status -H "X-Admin-Api-Key: test-cairoscan-admin-key"
```

## Step 6: Dynamic Chunking Engine (Bucket A)

### Architectural additions

- **`controllers/ChunkingController.py`** — one generic, two-step process for every sheet's
  already-staged Bucket-A rows, for every client:
  1. Concatenate every non-empty field into `"label: value"`, walking `schema_registry`'s
     discovered column order for that `(client_id, sheet_name)` — no exclusions, no statistical
     calculation of any kind.
  2. Only *then*, as a separate final step, prepend `"[Document: {file_name}] "` (extension
     stripped) to the finished string.

  Every chunk also carries `metadata.sheet_name` and `metadata.source_file`, and `chunk_type` is
  stamped with the sheet name — the same "never a per-sheet special case" discipline as Step 5.
- **`tasks/chunk_generation.py`** (`generate_chunks`) — chained directly off Step 5's
  `parse_and_stage` once staging succeeds. Delete-and-reinserts into `knowledge_chunks`, scoped to
  `(client_id, source_file)`, exactly like `staging_rows`. No embedding is generated yet — the
  `embedding` column stays `NULL` until Step 8's shootout picks a backend.
- **`celery_app.py`'s `get_setup_utils()`** now also returns `chunk_model` (a `ChunkModel`
  instance) alongside the models added in earlier steps.

### Why there's no boilerplate detection here

An earlier version of `ChunkingController` additionally computed each column's duplication rate
against a per-client `client_config.boilerplate_threshold` and excluded columns that cleared it,
stamping the excluded fields into `metadata` instead of the embedded text. That mechanism has been
**fully removed** after evaluating it against real `raylab` data: column-level exclusion would
have silently deleted a single branch's genuinely informative "no wheelchair access" exception
(measured duplication rate ~97.4% on that column, driven entirely by every *other* branch sharing
the same value), and excluded text moved into `metadata` is invisible to both dense embedding
*and* BM25/keyword search — `metadata` is only ever exact-match filterable, never free-text
searchable. The real data measured across `raylab`'s 17 synced sheets turned out to already be
dense and diverse (max column duplication ~26% outside that one small directory sheet), so the
theoretical "vector dilution" risk the mechanism was meant to guard against wasn't worth the
concrete, silent data-loss risk it introduced. See `claude.md` §3.8 for the full write-up. There
is nothing per-client to configure for chunking anymore — `client_config.boilerplate_threshold`
no longer exists as a column.

### Database migration — drop `client_config.boilerplate_threshold`

```bash
cd src/models/db_schemes/raylab
alembic revision --autogenerate -m "drop boilerplate_threshold from client_config"
alembic upgrade head
alembic current   # ff0b5276e5cb (head)
```

Confirm at the DB level:
```bash
docker exec raylab-pgvector psql -U postgres -d raylab -c "\d client_config"
```
Expect: no `boilerplate_threshold` row in the column list.

### Verifying through the real sync pipeline

With the API/worker running (§Step 4) and the real `raylab` OneDrive folder configured, trigger a
full sync in **Postman** (never curl/raw scripts, per claude.md §4.3):

1. `POST http://localhost:8000/api/sync` with header `X-Admin-Api-Key: <the real raylab admin key>`.
2. Poll `GET http://localhost:8000/api/sync/<task_id>/status` for each chained task
   (`fetch_and_dispatch` → `parse_and_stage` → `generate_chunks`, one chain per file in the shared
   folder) until every one reports `SUCCESS`.

Then confirm the result directly in Postgres (non-HTTP check, allowed under claude.md §4.3):
```bash
docker exec raylab-pgvector psql -U postgres -d raylab -c \
  "SELECT chunk_type, content, metadata FROM knowledge_chunks WHERE client_id = 'raylab' LIMIT 5;"
```

Expect, for every row:
- `content` starts with `[Document: <file name without extension>] ` followed immediately by the
  first populated field as `label: value`, then `. `-joined subsequent fields — nothing skipped
  except genuinely empty cells.
- `metadata` contains only `sheet_name` and `source_file` — **no** `boilerplate_excluded_fields`
  key exists anymore, on any row, for any sheet.
- Row count for a given `source_file` matches its staged row count in `staging_rows` exactly (no
  exclusions means no row/column ever silently disappears from the embedded text).

## Step 7: Vector DB Storage Layer (Multi-Tenant pgvector)

### Architectural additions

- **`stores/vectordb/VectorDBInterface.py`** — the port: `insert_many(client_id, chunks)` and
  `search_by_vector(client_id, query_vector, top_k, metadata_filters)`. `client_id` is required, no
  default, on both — it is impossible to call either without a tenant scope, enforced at the
  signature itself (claude.md §1.3).
- **`stores/vectordb/providers/PGVectorProvider.py`** — the concrete adapter. Every query is
  built with `.where(KnowledgeChunk.client_id == client_id)` at the SQLAlchemy level (never a
  filter applied after the fact), plus `.where(KnowledgeChunk.embedding.isnot(None))` so
  not-yet-embedded rows (everything, until Step 8) are excluded rather than erroring or sorting
  arbitrarily. Similarity ordering uses pgvector's `cosine_distance()` (the `<=>` operator),
  matching `idx_chunks_embedding_hnsw`'s `vector_cosine_ops`. When `metadata_filters` is given, it's
  applied as a JSONB containment match (`metadata @> filters`) — never a free-text match.
- **`stores/vectordb/VectorDBEnums.py`** / **`VectorDBProviderFactory.py`** — the same config-driven
  selection pattern as `stores/onedrive`. Swapping pgvector for a hosted vector DB later is one new
  `providers/` file plus one branch in the factory — `ChunkModel` and every controller stay
  untouched.
- **`models/ChunkModel.py`** — `insert_many_chunks` and the new `search_by_vector` no longer run
  their own SQL; both delegate to the injected `vectordb_client`. `create_chunk`,
  `delete_chunks_by_source_file`, and `get_total_chunks_count` are unchanged (they're plain
  relational operations, not vector-specific, so they stay as direct repository methods).
- **`celery_app.py`'s `get_setup_utils()`** now builds a `VectorDBProviderFactory`, creates the
  `vectordb_client` from `settings.VECTOR_DB_BACKEND`, injects it into `ChunkModel`, and returns it
  in the dict too (for direct use once Step 9's `RetrievalController` needs it). `main.py` is
  **not** touched — the API process has no route that needs vector search yet (that's Step 9).
- **`helpers/config.py`** gained `VECTOR_DB_BACKEND_LITERAL` / `VECTOR_DB_BACKEND` (default
  `PGVECTOR`), identical pattern to `ONEDRIVE_AUTH_BACKEND`.

No database migration in this step — `knowledge_chunks`/`embedding` already existed from Step 1;
Step 7 only adds an access-layer abstraction on top of it. `celery_app.py`'s composition root did
change, though — **restart any running Celery worker** before relying on this step's behavior in
the live pipeline (a standalone verification script, like the one below, always picks up the
current code since it imports fresh on each run, so this only matters for a long-lived worker
process you started before this change).

### Two things this step's test *can't* prove yet, and why

1. **No real embeddings exist yet.** Step 6 deliberately leaves `embedding` `NULL` on every chunk
   — that only gets populated in Step 8's shootout. So `search_by_vector`'s *semantic* relevance
   can't be verified yet; what Step 7 verifies is that the storage/query **plumbing** (SQL
   correctness, cosine ordering, `client_id` isolation) works. The script below proves that using
   temporary placeholder vectors on **real, already-synced `raylab` content rows** it creates and
   deletes itself — not fabricated business data, just numeric probes to exercise the SQL, deleted
   at the end of the run. Real relevance testing is Step 8/9's job.
2. **`cairoscan`/`technoscan` are not separate `client_id`s in this real environment.** Per
   claude.md §3.4, they're two brand values inside `client_id='raylab'`'s own data (the `Account`
   column, read at parse time) — there's only one real tenant onboarded right now. So the isolation
   test below proves the same property the Implementation Plan asks for (zero cross-tenant leakage)
   using a second, clearly-labeled probe `client_id` instead of a real second client — legitimate
   under claude.md §4.3's carve-out for edge-case probes against real infrastructure.

### Verifying — real content, temporary probe vectors, real infrastructure

Run from `src/`, conda env active (this reuses `celery_app.py`'s actual composition root, so it
exercises the real `ChunkModel` → `VectorDBProviderFactory` → `PGVectorProvider` wiring, not a
stand-in):

```bash
cd /mnt/d/Raylab_Project/src
python <<'EOF'
import asyncio

from celery_app import get_setup_utils
from models.db_schemes.raylab.schemes import KnowledgeChunk

DIM = 1024
PROBE_SOURCE_FILE = "__step7_verification_probe__"


def vector(active_index: float, magnitude: float = 1.0) -> list[float]:
    v = [0.0] * DIM
    v[0] = magnitude
    v[1] = active_index
    return v


async def main():
    setup = await get_setup_utils()
    chunk_model = setup["chunk_model"]

    try:
        await chunk_model.delete_chunks_by_source_file(client_id="raylab", source_file=PROBE_SOURCE_FILE)
        await chunk_model.delete_chunks_by_source_file(client_id="__step7_isolation_probe__", source_file=PROBE_SOURCE_FILE)

        raylab_close = KnowledgeChunk(
            client_id="raylab", content="[probe] raylab close vector",
            source_file=PROBE_SOURCE_FILE, chunk_type="__probe__",
            embedding=vector(active_index=0.05), metadata_payload={},
        )
        raylab_far = KnowledgeChunk(
            client_id="raylab", content="[probe] raylab far vector",
            source_file=PROBE_SOURCE_FILE, chunk_type="__probe__",
            embedding=vector(active_index=0.9), metadata_payload={},
        )
        other_tenant = KnowledgeChunk(
            client_id="__step7_isolation_probe__", content="[probe] other-tenant vector",
            source_file=PROBE_SOURCE_FILE, chunk_type="__probe__",
            embedding=vector(active_index=0.0), metadata_payload={},
        )

        inserted = await chunk_model.insert_many_chunks(client_id="raylab", chunks=[raylab_close, raylab_far])
        inserted_other = await chunk_model.insert_many_chunks(client_id="__step7_isolation_probe__", chunks=[other_tenant])
        print(f"1) inserted {inserted} raylab probe rows, {inserted_other} other-tenant probe row")

        query_vector = vector(active_index=0.0)  # closest to other_tenant, then raylab_close, then raylab_far
        results = await chunk_model.search_by_vector(client_id="raylab", query_vector=query_vector, top_k=5)

        result_ids = [str(r.id) for r in results]
        print(f"2) search_by_vector(client_id='raylab') returned {len(results)} rows")
        assert all(r.client_id == "raylab" for r in results)
        assert str(raylab_close.id) in result_ids and str(raylab_far.id) in result_ids
        assert str(other_tenant.id) not in result_ids
        print("   PASS -- other tenant's row does NOT appear, even though it was vector-closest")

        assert results[0].id == raylab_close.id
        print("   PASS -- ordering correct: closer probe vector ranked before the farther one")

        try:
            await chunk_model.search_by_vector(query_vector=query_vector, top_k=5)
            print("3) FAIL: search_by_vector ran without client_id")
        except TypeError as e:
            print(f"3) PASS -- search_by_vector refuses to run without client_id: {e}")

        try:
            await chunk_model.insert_many_chunks(chunks=[raylab_close])
            print("4) FAIL: insert_many_chunks ran without client_id")
        except TypeError as e:
            print(f"4) PASS -- insert_many_chunks refuses to run without client_id: {e}")

        print("ALL CHECKS PASSED")

    finally:
        await chunk_model.delete_chunks_by_source_file(client_id="raylab", source_file=PROBE_SOURCE_FILE)
        await chunk_model.delete_chunks_by_source_file(client_id="__step7_isolation_probe__", source_file=PROBE_SOURCE_FILE)
        await setup["db_engine"].dispose()


asyncio.run(main())
EOF
```

Expect `ALL CHECKS PASSED` with all four numbered checks printing `PASS`.

Confirm no residue was left behind, and that the real `raylab` row count is untouched:
```bash
docker exec raylab-pgvector psql -U postgres -d raylab -c \
  "SELECT count(*) FROM knowledge_chunks WHERE source_file = '__step7_verification_probe__' OR client_id = '__step7_isolation_probe__';"
# expect 0

docker exec raylab-pgvector psql -U postgres -d raylab -c \
  "SELECT count(*) FROM knowledge_chunks WHERE client_id = 'raylab';"
# expect the same real count as before this script ran (e.g. 1869 as of this writing)
```

## Step 8: Embedding Model Shootout (BAAI/bge-m3 vs. Swan-Large)

### Real research findings that shaped this step

Before writing any code, two real, concrete blockers were found while researching Swan-Large
(`UBC-NLP/swan-large`), the proposal's named second candidate:

1. **Its HuggingFace repo is almost certainly gated.** Both `huggingface.co/UBC-NLP/swan-large`
   and its API endpoint (`/api/models/UBC-NLP/swan-large`) return `401 Unauthorized` — normal
   public model pages don't do that. Downloading it for real needs a HuggingFace account,
   accepting a license/terms agreement, and a valid `HF_TOKEN`.
2. **It's architecturally much heavier than the proposal assumed.** The published paper
   (arXiv:2411.01192) confirms Swan-Large is built on **ArMistral-7B** — a 7-billion-parameter
   Mistral-based Arabic LLM — not a lightweight BERT-style encoder like BGE-M3 (~580MB). A
   7B-parameter model needs roughly 14GB+ just to load its weights in fp16. This dev machine's
   actual hardware was checked directly: GPU is a **Quadro M2200 with 4GB VRAM**, and **WSL2 is
   allocated only 3.7GB of total RAM** — both far short of what Swan-Large needs, independent of
   the gating issue.

Given both blockers, the decision (confirmed with the user) was: **build the complete,
swappable architecture now; BGE-M3 fully working; Swan-Large wired into the same interface but
deliberately failing loudly rather than fabricating output**, so promoting a real, working
Swan-Large later — once gated access and adequate hardware exist — is a config change, not a
rewrite.

### Architectural additions

- **`stores/llm/LLMInterface.py`** — the port: `embed_text(texts, is_query)` and an
  `embedding_dimension` property. `ChunkingController` (a future wiring, not yet done — see below)
  and `RetrievalController` (Step 9) will only ever call this interface, never a specific model
  class.
- **`stores/llm/providers/BGEM3Provider.py`** — real, working. Lazily loads
  `BAAI/bge-m3` via `sentence-transformers` on first use (class-level cache, shared across
  instances — the model is thread-safe for inference), prepends the model's documented query
  instruction prefix only to query texts (never document texts), and always encodes with
  `normalize_embeddings=True` (required for cosine similarity to be meaningful). 1024-dim output —
  matches `knowledge_chunks.embedding`'s existing column with no schema change.
- **`stores/llm/providers/SwanLargeProvider.py`** — deliberately non-functional right now. Its
  `_get_model()`/`embedding_dimension` both raise `SwanLargeUnavailableError` with the exact
  research findings above, rather than guessing a prefix convention or a fake dimension. The
  proposal itself (Implementation Plan, Step 8) explicitly warns against assuming BGE-M3's prefix
  rules apply to Swan-Large without checking its own model card — since gated access blocks
  checking that, this class leaves `QUERY_PREFIX = ""` marked `UNCONFIRMED` rather than guessing.
- **`stores/llm/LLMEnums.py`** / **`LLMProviderFactory.py`** — same config-driven pattern as
  `stores/onedrive`/`stores/vectordb`. Both `BGE_M3` and `SWAN_LARGE` are registered even though
  the latter can't run yet — the enum/factory represent the interface contract, not current
  runnability.
- **`controllers/EmbeddingShootoutController.py`** — orchestration only. For each candidate model:
  embeds this client's **entire real, already-chunked content** (`ChunkModel.get_all_chunks`) into
  that model's own **in-memory scratch pool** (a plain NumPy matrix — never
  `knowledge_chunks.embedding` itself, since the production column belongs to whichever model is
  eventually promoted, and candidates aren't guaranteed to share its dimensionality); scores top-K
  retrieval accuracy against `evaluation_queries` using a plain dot product (mathematically cosine
  similarity here, since every provider normalizes its output). A candidate that raises during
  loading (Swan-Large, right now) is caught **per-model** — one candidate's failure never blocks
  the other's real result from being scored and persisted.
- **`models/EvaluationQueryModel.py`** + **`evaluation_queries`**/**`shootout_results`** tables —
  the shared benchmark query set and its scored results. `evaluation_queries` pairs a real
  Egyptian-Arabic-style question with the real `knowledge_chunks.id` that answers it — grounded in
  this client's actual synced content, not fabricated business facts (claude.md §4.3); only the
  questions themselves are a designed evaluation harness, exactly as the proposal describes for
  bootstrapping a benchmark before real user query logs exist. `shootout_results.top_k_accuracy`
  and `.error_message` are both nullable — a candidate that can't run still gets a row recording
  *why*, never a silently missing result.
- **`tasks/embedding_shootout.py`** (`run_shootout`) — the Celery task. Time limit overridden to
  1800s (a first run also downloads BGE-M3's weights and CPU-encodes the client's full real chunk
  set). No HTTP route exists for this yet, matching the Implementation Plan (Step 8 lists no
  route) — it's triggered directly via `.delay()`, the same way earlier steps' verification
  scripts have always invoked tasks directly when no endpoint exists for them.
- **`ChunkModel.get_all_chunks(client_id)`** — new plain repository read (not vector-specific, so
  it doesn't delegate to `vectordb_client`) backing the controller's scratch-pool construction.
- **`helpers/config.py`** gained `EMBEDDING_BACKEND` (default `BGE_M3` — the one that actually
  works right now) and `HF_TOKEN` (optional, only consumed by `SwanLargeProvider`).
- **`celery_app.py`** — composition root now also builds `EvaluationQueryModel` and registers
  `tasks.embedding_shootout` + its own queue; `get_setup_utils()`'s dict also exposes `settings`
  directly now (the task needs `EMBEDDING_BACKEND_LITERAL` and `HF_TOKEN`, not just the models).

**Not wired up yet, on purpose:** `ChunkingController`/`chunk_generation.py` still leave
`embedding` `NULL` — this step is the shootout only. Promoting a winner to actually populate
`knowledge_chunks.embedding` on every sync is a follow-on wiring change once a real winner is
chosen (i.e. once Swan-Large either becomes real, or BGE-M3 wins by default) — doing it now would
mean writing production embeddings from a benchmark that hasn't run its second candidate for real
yet.

### New dependencies

`torch` **must** be installed from PyTorch's CPU-only wheel index *before* the rest of
`requirements.txt` — a plain `pip install torch` on Linux pulls the full CUDA dependency bundle
(cublas, cudnn, etc. — 2GB+ of `nvidia-*` wheels) by default, which this dev machine can't even
download: WSL2's `/tmp` is a RAM-backed `tmpfs` capped at ~1.9GB (matching its 3.7GB total RAM
allocation), so the CUDA download blows past it with `OSError: No space left on device` even
though the real disk has hundreds of GB free. The CPU-only wheel avoids this entirely — it's a
fraction of the size and doesn't touch `/tmp` for anything close to that long:

```bash
conda activate raylab
cd /mnt/d/Raylab_Project/src
pip install --index-url https://download.pytorch.org/whl/cpu torch==2.5.1
pip install -r requirements.txt   # torch already satisfied; installs sentence-transformers, numpy
```
This machine's GPU (Quadro M2200, 4GB VRAM) offers no real benefit for BGE-M3 anyway and is
nowhere near enough for Swan-Large regardless — install a real CUDA build yourself only if you
move this to GPU hardware that can actually use it.

### Database migration — `evaluation_queries` + `shootout_results`

```bash
cd src/models/db_schemes/raylab
alembic revision --autogenerate -m "create evaluation_queries and shootout_results"
alembic upgrade head
alembic current   # 83b89604b9c4 (head)
```

Confirm at the DB level:
```bash
docker exec raylab-pgvector psql -U postgres -d raylab -c "\d evaluation_queries"
docker exec raylab-pgvector psql -U postgres -d raylab -c "\d shootout_results"
```

### Verifying — real content, real BGE-M3, Swan-Large's expected real failure

This seeds a real, grounded benchmark query set (8 questions, each paired with a genuine
`knowledge_chunks.id` already synced from `raylab`'s real OneDrive content across four different
real sheets), then runs the actual shootout. Run from `src/`, conda env active — **the first run
downloads BGE-M3's ~580MB weights and CPU-encodes the client's full real chunk set, so expect this
to take a few minutes**:

```bash
cd /mnt/d/Raylab_Project/src
python <<'EOF'
import asyncio

from celery_app import get_setup_utils
from controllers.EmbeddingShootoutController import EmbeddingShootoutController
from stores.llm.LLMProviderFactory import LLMProviderFactory

CLIENT_ID = "raylab"

QUERIES = [
    {"query_text": "هل فرع المهندسين فيه كرسي متحرك؟", "expected_chunk_id": "3055e5f4-f24e-4ff3-b88a-a5df5417a3f1"},
    {"query_text": "مواعيد المعمل في فرع الجيزة يوم الجمعة ايه؟", "expected_chunk_id": "ed564227-a909-4439-bed9-b713864e9acf"},
    {"query_text": "هل فيه اسانسير في فرع اكتوبر؟", "expected_chunk_id": "80d45936-2b8b-440d-83b7-5d3e9d05d25e"},
    {"query_text": "هل ينفع اشرب مية قبل سونار البطن والحوض؟", "expected_chunk_id": "7299fb09-6146-442a-ab00-1d6dd6a5c670"},
    {"query_text": "عايز اعرف التحضير المطلوب لسونار البروستاتا عن طريق الشرج", "expected_chunk_id": "94dcf2ca-732b-40f8-883f-abe942f27bcb"},
    {"query_text": "انا عضو نقابة المهندسين هل ممكن احضر الموافقة من الفرع؟", "expected_chunk_id": "9641cc47-20c1-4434-b7fb-0ae248ef182e"},
    {"query_text": "نقابة تجاريين القاهرة الموافقة بتتحضر منين، من الفرع ولا النقابة؟", "expected_chunk_id": "452351f5-68b2-408f-82a1-100298811a8d"},
    {"query_text": "عايز اعرف تفاصيل تحليل فحص الخلايا عن طريق سائل من الجسم", "expected_chunk_id": "f5e1c973-c23e-47cb-8a9b-250d4601b881"},
]


async def main():
    setup = await get_setup_utils()
    settings = setup["settings"]
    evaluation_query_model = setup["evaluation_query_model"]

    try:
        seeded = await evaluation_query_model.seed_queries(client_id=CLIENT_ID, queries=QUERIES)
        print(f"1) seeded {seeded} real, grounded benchmark queries for client_id={CLIENT_ID!r}")

        controller = EmbeddingShootoutController(
            chunk_model=setup["chunk_model"],
            evaluation_query_model=evaluation_query_model,
            llm_provider_factory=LLMProviderFactory(config=settings),
        )

        total_chunks = await setup["chunk_model"].get_total_chunks_count(CLIENT_ID)
        print(f"2) running shootout against {total_chunks} real chunks -- this can take a few minutes")
        results = await controller.run_shootout(client_id=CLIENT_ID, model_names=settings.EMBEDDING_BACKEND_LITERAL, top_k=5)

        for r in results:
            if r["error"] is None:
                print(f"   PASS -- {r['model_name']}: top_5_accuracy = {r['top_k_accuracy']:.3f}")
            else:
                print(f"   EXPECTED FAILURE -- {r['model_name']}: {r['error'][:200]}")

        assert any(r["model_name"] == "BGE_M3" and r["error"] is None for r in results)
        assert any(r["model_name"] == "SWAN_LARGE" and r["error"] is not None for r in results)
        print("ALL CHECKS PASSED")

    finally:
        await setup["db_engine"].dispose()


asyncio.run(main())
EOF
```

Confirm at the DB level:
```bash
docker exec raylab-pgvector psql -U postgres -d raylab -c \
  "SELECT model_name, top_k_accuracy, query_count, top_k, left(error_message, 80) AS error FROM shootout_results WHERE client_id='raylab' ORDER BY evaluated_at;"
```
Expect: one `BGE_M3` row with a real, non-null `top_k_accuracy` (a real number between 0 and 1 —
not a fabricated/expected value, whatever the actual model produces against this real query set),
and one `SWAN_LARGE` row with `top_k_accuracy = NULL` and a populated `error_message` explaining
the gated-access/hardware blocker — a recorded, explained failure, never a silently missing row.

### If/when Swan-Large becomes real

Once gated HuggingFace access is granted (accept the license on its model page, generate a token)
and this runs on hardware with enough VRAM/RAM: set `HF_TOKEN` in `.env`, confirm Swan-Large's real
query/document prefix convention from its actual model card (never assume BGE-M3's), fill in
`SwanLargeProvider.QUERY_PREFIX` and `embedding_dimension` for real, and re-run the script above —
no other file changes, since both candidates already sit behind the same `LLMInterface`.

### Automatic Embedding Generation (promoting BGE-M3 to production)

Steps 5–8, as originally built, left a real gap: `chunk_generation.py` wrote every real chunk
with `embedding = NULL`, and Step 8's shootout deliberately never wrote back to that column (it
only ever built its own in-memory scratch pool — see above). Nothing in the sync pipeline actually
populated production embeddings. This section closes that gap.

**What was added:**
- **`stores/vectordb/VectorDBInterface.py` / `PGVectorProvider.py`** gained `update_embeddings(client_id, embeddings)` —
  bulk-writes computed vectors back onto existing rows, still scoped by `client_id` at the SQL level.
- **`models/ChunkModel.py`** gained `update_embeddings` (delegates to the vectordb adapter) and
  `get_chunks_without_embedding(client_id, source_file=None)` (a plain relational read — filtering
  on `embedding IS NULL`, not comparing vectors — so it stays a direct repository method).
- **`controllers/EmbeddingGenerationController.py`** — `embed_chunks(client_id, source_file=None)`.
  Fetches whatever's missing an embedding, embeds it in small batches via whichever provider it's
  handed (`settings.EMBEDDING_BACKEND` via `LLMProviderFactory` — never constructed by the
  controller itself), and **commits incrementally, one batch at a time**. If interrupted partway
  (crash, Celery time-limit kill, `Ctrl+C`), already-embedded chunks are never lost, and re-running
  the exact same call only ever processes what's *still* missing — the selection criterion is
  always `embedding IS NULL`, never a positional offset that could skip or repeat rows.
- **`tasks/embedding_generation.py`** (`generate_embeddings`) — the Celery task. Two ways it's used:
  1. **Automatic, chained**: `tasks/chunk_generation.py` now calls `generate_embeddings.delay(client_id=client_id, source_file=source_file)` right after inserting a sync's chunks — every future sync
     embeds its own freshly-chunked rows with no manual step.
  2. **Manual backfill**: call the exact same task **without `source_file`** to process every chunk
     across a client's whole history still missing an embedding — this covers everything synced
     before this wiring existed. Same task, same controller, same code — just a wider scope.
- **`client_config` schema unchanged** — this addition needed no new migration.

**A real, honest caveat about hardware**: this dev machine's BGE-M3 CPU encoding is extremely slow
(the Step 8 shootout above took multiple hours to encode this same ~1,869-chunk corpus). A full
backfill will likely take a very long time here. This is why `generate_embeddings`' task time limit
is set to 21600s (6h, overriding the global 600s default) — and why being interrupted is *safe*:
thanks to the incremental-commit design, re-running the identical command afterward simply resumes
from whatever's still `NULL`, never redoing already-embedded rows or losing progress.

### Running the backfill for existing data

```bash
# 1. Apply the migration if you haven't already (no-op if already at head)
cd src/models/db_schemes/raylab
alembic upgrade head
alembic current   # 32f61443e199 (head)

# 2. Restart the Celery worker so it picks up the new task + queue
cd /mnt/d/Raylab_Project/src
celery -A celery_app worker --queues=default,onedrive_sync,document_parsing,chunk_generation,embedding_shootout,embedding_generation --loglevel=info
```

In a separate terminal, enqueue the backfill (no `source_file` = every un-embedded chunk for this client):
```bash
cd /mnt/d/Raylab_Project/src
python <<'EOF'
from tasks.embedding_generation import generate_embeddings
task = generate_embeddings.delay(client_id="raylab")
print("enqueued task_id:", task.id)
EOF
```

Watch real, incremental progress either in the Celery worker's own log (`embedded X/Y chunks so
far` lines), or by re-running this query periodically — the count only ever goes up, never resets:
```bash
docker exec raylab-pgvector psql -U postgres -d raylab -c \
  "SELECT count(*) AS total, count(embedding) AS with_embedding FROM knowledge_chunks WHERE client_id='raylab';"
```
Expect `with_embedding` to climb from `0` toward `total` over time. If it stalls or the task gets
killed by the time limit, just re-run the `generate_embeddings.delay(...)` script above — it picks
up exactly where it left off.

### Testing that automatic embedding works for future syncs

Trigger a real sync in Postman as usual (`POST /api/sync`), then poll the chained task IDs in order
— `fetch_and_dispatch` → `parse_and_stage` → `generate_chunks` → **`embedding_task_id`** (new, in
`generate_chunks`'s result) — until the last one reports `SUCCESS`. Confirm at the DB level that the
specific file you just synced now has embeddings:
```bash
docker exec raylab-pgvector psql -U postgres -d raylab -c \
  "SELECT count(*) AS total, count(embedding) AS with_embedding FROM knowledge_chunks WHERE client_id='raylab' AND source_file='<the file you synced>';"
```
Expect `with_embedding = total` for that file once the embedding task finishes — no manual backfill
step needed for anything synced from now on.

## Step 9: Hybrid Retrieval Endpoint

### Architectural additions

- **`routes/schemes/retrieval.py`** — `RetrieveRequest` (`query`, optional `metadata_filters`) /
  `RetrieveResponse` (`client_id`, `query`, `results: list[RetrievedChunk]`). `top_k` is
  deliberately **not** a request field — it's a per-tenant `client_config.retrieval_top_k` value,
  never something a caller can override (claude.md §1.3).
- **`routes/retrieval.py`** — `POST /api/retrieve`. `client_id` is resolved server-side from the
  `X-Admin-Api-Key` header — the exact same mechanism `routes/sync.py` already uses
  (`ClientConfigModel.get_client_id_by_admin_api_key`). Section 2 has no broader session/auth
  system in scope (the Implementation Plan's "resolves client_id from session context" language
  refers to Section 3/4's WhatsApp/Voice context-aware routing, which is explicitly out of scope —
  claude.md §2.1); reusing the already-established admin-key mechanism was the deliberate choice
  over inventing a second, parallel auth path for one endpoint. Transport only — no DB session or
  business logic touched directly.
- **`controllers/RetrievalController.py`** — orchestration only:
  1. Fetches `client_config`, validates any `metadata_filters` keys against
     `client_config.allowed_metadata_keys` — an unrecognized key raises `MetadataFilterValidationError`
     (mapped to `422`), never silently ignored or matched against nothing (claude.md §3.4).
  2. Embeds the query via `app.embedding_client.embed_text(is_query=True)`.
  3. Calls `chunk_model.hybrid_search(...)` for the fused dense+sparse candidate pool.
  4. Re-ranks that pool via `app.reranker_client.rerank(...)`, returns the final `retrieval_top_k`.
- **`stores/vectordb/providers/PGVectorProvider.py`** gained `search_by_bm25` (Postgres full-text
  search using the `'simple'` config — stock Postgres has no Arabic stemming dictionary, so
  `'simple'` tokenize-and-lowercase is the honest choice, not `'english'`) and `hybrid_search`
  (runs both legs, fuses via standard Reciprocal Rank Fusion: `score = Σ 1/(rrf_k + rank)` over
  whichever leg(s) a chunk appears in). If the dense leg is empty (no embeddings yet for this
  client), fusion gracefully degrades to sparse-only ranking — never an error.
- **`stores/reranker/*`** — `RerankerInterface`/`RerankerEnums`/`RerankerProviderFactory`/
  `providers/CrossEncoderProvider.py`, the same Ports & Adapters pattern as `stores/llm`/
  `stores/vectordb`. Uses `cross-encoder/mmarco-mMiniLMv2-L12-H384-v1` — small (~470MB), ungated,
  multilingual (mMARCO covers Arabic) — deliberately not a larger reranker, since re-ranking runs
  synchronously on every retrieval request's latency path, and this dev machine's real hardware
  constraints make request-time latency a hard concern here, not a nice-to-have.
- **`client_config`** gained `retrieval_top_k` (default `5`) and `rrf_k` (default `60`) — claude.md
  §1.3 names top-K and RRF's k explicitly as values that must never be hardcoded Python constants.
  The candidate pool size fed into fusion (`candidate_k`) is a derived multiple of `top_k` computed
  in code, not a separate config value — it's an internal quality/performance tradeoff, not a
  per-tenant business decision the way top-K and RRF's k are.
- **`main.py`** — the API composition root now also builds `vectordb_client`, `chunk_model`,
  `embedding_client`, `reranker_client`, and `retrieval_controller` at startup, and includes
  `retrieval.retrieval_router`. `embedding_client`/`reranker_client` are loaded once at process
  startup and held for the process lifetime (proposal §Step 4: "load the production model once at
  startup... a single sentence embeds in ~15ms" — `SentenceTransformer`/`CrossEncoder` instances
  are thread-safe for inference, shared across every request).

No database migration needed beyond the `retrieval_top_k`/`rrf_k` columns above — `knowledge_chunks`
already had everything else from Step 1.

### Before you start the API: a sequencing note

`main.py`'s startup now loads BGE-M3 (for query embedding) and the cross-encoder (for re-ranking)
into memory. **Don't start `uvicorn` while a Step 8 shootout run or the embedding backfill is still
active** — this machine has already hit real memory limits twice; loading a second BGE-M3 instance
on top of one still resident risks another crash. Check first:
```bash
ps aux | grep step8_seed | grep -v grep    # or whatever process is currently embedding
```

### Verifying — real BM25, real RRF, real re-ranking, real isolation (dense leg honest about its current state)

I proved the SQL-level logic directly against the live database before writing this section:
`search_by_bm25` found real matches for a real Arabic query against real `raylab` content;
`search_by_vector` correctly returned zero rows (no embeddings exist for most of the corpus until
the backfill above finishes) without erroring; `hybrid_search` gracefully degraded to BM25-only
ranking and produced the identical result set; and a controlled isolation probe (a second,
clearly-fake `client_id`) returned nothing. Once the backfill/automatic embedding above has run for
a given file, its dense leg contributes real results too — nothing else changes.

**CLI (once the API is running):**
```bash
conda activate raylab
cd /mnt/d/Raylab_Project/src
uvicorn main:app --reload --port 8000
curl http://localhost:8000/api/
```

**Postman:**

| # | Request | Expected |
|---|---|---|
| 1 | `POST /api/retrieve`, header `X-Admin-Api-Key: <raylab's real key>`, body `{"query": "نقابة المهندسين"}` | `200`, real content back |
| 2 | Same, body adds `"metadata_filters": {"sheet_name": "Insurance Guide"}` | `200`, results narrowed to that real sheet |
| 3 | Same, body adds `"metadata_filters": {"not_a_real_key": "x"}` | `422` |
| 4 | Same as #1 with header `X-Admin-Api-Key: not-a-real-key` | `401` |

Confirm isolation for request #1's returned `id`s:
```bash
docker exec raylab-pgvector psql -U postgres -d raylab -c \
  "SELECT id, client_id FROM knowledge_chunks WHERE id IN (<ids from the response>);"
```
Expect every row to show `client_id = 'raylab'`.

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
| `EMBEDDING_BACKEND_LITERAL` | `src/.env` | Self-documenting list of implemented embedding backends |
| `EMBEDDING_BACKEND` | `src/.env` | Which embedding backend is production (default `BGE_M3` — the one that actually works) |
| `HF_TOKEN` | `src/.env` | Optional HuggingFace token for gated repos (Swan-Large only; BGE-M3 ignores this) |
| `VECTOR_DB_BACKEND` / `VECTOR_DB_BACKEND_LITERAL` | `src/.env` | Vector DB provider selection (currently only `PGVECTOR`) |
| `RERANKER_BACKEND` / `RERANKER_BACKEND_LITERAL` | `src/.env` | Re-ranker provider selection (currently only `CROSS_ENCODER`) |
| `CELERY_BROKER_URL` / `CELERY_RESULT_BACKEND` / `CELERY_TASK_*` | `src/.env` | Celery task queue config (Step 4) |

## Project layout

See `claude.md` §1.1 for the full target directory tree. All nine steps exist today:

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
│   ├── helpers/config.py       # Postgres + MSAL/OneDrive + Celery + embedding backend literals
│   ├── routes/
│   │   ├── base.py
│   │   ├── sync.py
│   │   ├── retrieval.py
│   │   └── schemes/{sync,retrieval}.py
│   ├── controllers/
│   │   ├── BaseController.py
│   │   ├── SyncController.py
│   │   ├── DocumentParsingController.py
│   │   ├── ChunkingController.py
│   │   ├── EmbeddingShootoutController.py
│   │   ├── EmbeddingGenerationController.py
│   │   └── RetrievalController.py
│   ├── tasks/
│   │   ├── onedrive_sync.py
│   │   ├── document_parsing.py
│   │   ├── chunk_generation.py
│   │   ├── embedding_shootout.py
│   │   └── embedding_generation.py
│   ├── stores/
│   │   ├── onedrive/
│   │   │   ├── OneDriveInterface.py, OneDriveEnums.py, OneDriveProviderFactory.py
│   │   │   └── providers/MSALGraphProvider.py
│   │   ├── vectordb/
│   │   │   ├── VectorDBInterface.py, VectorDBEnums.py, VectorDBProviderFactory.py
│   │   │   └── providers/PGVectorProvider.py   # insert_many, search_by_vector, search_by_bm25,
│   │   │                                       #   hybrid_search (RRF), update_embeddings
│   │   ├── reranker/
│   │   │   ├── RerankerInterface.py, RerankerEnums.py, RerankerProviderFactory.py
│   │   │   └── providers/CrossEncoderProvider.py
│   │   └── llm/
│   │       ├── LLMInterface.py, LLMEnums.py, LLMProviderFactory.py
│   │       ├── providers/BGEM3Provider.py (real, working), providers/SwanLargeProvider.py (deferred)
│   │       └── templates/clients/<client_id>/   # generated, gitignored — Bucket B/C (§3.5)
│   │           ├── prompt_templates.py
│   │           └── system_directives.py
│   ├── utils/
│   │   ├── dynamic_schema_loader.py   # stateless auto-discovery call site
│   │   └── template_file_writer.py    # the only code allowed to write templates/clients/*
│   └── models/
│       ├── BaseDataModel.py
│       ├── ChunkModel.py              # + hybrid_search, update_embeddings, get_chunks_without_embedding
│       ├── ClientConfigModel.py       # + get_client_id_by_admin_api_key
│       ├── SchemaRegistryModel.py     # get_or_register / update_bucket / set_mandatory_fields
│       ├── TokenCacheModel.py         # Fernet-encrypted, client_id-scoped
│       ├── StagingRowModel.py         # Bucket A only
│       ├── EvaluationQueryModel.py    # Step 8 benchmark query set + scored results
│       ├── enums/BucketEnum.py
│       └── db_schemes/raylab/
│           ├── schemes/{raylab_base,knowledge_chunk,client_config,schema_registry,token_cache,staging_row,evaluation_query,shootout_result}.py
│           ├── alembic.ini.example
│           └── alembic/{env.py, script.py.mako, versions/}
├── .gitignore
├── claude.md
└── README.md
```
