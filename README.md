# Raylab

Multi-tenant RAG backend (Section 2: OneDrive Integration). See `claude.md` for the full
architectural constitution and `D:\project\Implementation Plan — Section 2 The Multi-Tenant.md`
for the build order this project follows step by step.

## Status

**Step 5 of 9 complete: Dynamic 1NF Sheet Parsing & Bucket Routing.**

Implemented so far:
- **Step 1** — `knowledge_chunks` (pgvector store, `client_id NOT NULL`, `embedding VECTOR(1024)`), `client_config` (tenant registry), `ChunkModel`, `ClientConfigModel`, `BucketEnum`.
- **Step 2** — `helpers/config.py` extended (`EMBEDDING_BACKEND_LITERAL`, `DEFAULT_BOILERPLATE_THRESHOLD`); `schema_registry` table + `SchemaRegistryModel` (auto-discovery/auto-registration, upsert-on-discovery, zero manual DB entry); stateless `utils/dynamic_schema_loader.py`.
- **Step 3** — `stores/onedrive/*` (Interface/Enums/Factory/`MSALGraphProvider`), `TokenCacheModel` (Fernet-encrypted, DB-backed token cache, `client_id`-scoped, never plaintext).
- **Step 4** — `main.py` + `celery_app.py` (the two composition roots), `routes/sync.py` (`POST /api/sync`, `GET /api/sync/{task_id}/status`), `controllers/SyncController.py`, `tasks/onedrive_sync.py`. Redis/RabbitMQ added as Docker services; **no celery-beat service, no `beat_schedule` entry** — sync stays human-initiated only.
- **Step 5** — `controllers/DocumentParsingController.py` (auto-discovers/auto-registers sheets, routes rows by `BucketEnum`, stamps every row with its `sheet_name`); `tasks/document_parsing.py` (chained after `fetch_and_dispatch`); `staging_rows` table + `StagingRowModel` for **Bucket A only**. Bucket B/C are never written to the database — `utils/template_file_writer.py` renders them into generated `stores/llm/templates/clients/<client_id>/{prompt_templates,system_directives}.py` modules instead (see the Step 5 section below).
- Five chained Alembic migrations: `7ec06e4ca8ba` (Step 1) → `48d3c854b890` (Step 2) → `5ef02a92a86f` (Step 3) → Step 4's `client_config.admin_api_key` → Step 5's `staging_rows` table (see the Step 5 section below for its revision id).

Not built yet (later steps): chunking engine (Bucket A boilerplate detection), vector storage layer, embedding shootout, retrieval endpoint, `TemplateParser` extension (reads the Bucket B/C files Step 5 writes — Step 9's job). Do not assume any of these exist.

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

See `claude.md` §1.1 for the full target directory tree. Only the Steps 1–5 subset exists today:

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
│   │   ├── SyncController.py
│   │   └── DocumentParsingController.py
│   ├── tasks/
│   │   ├── onedrive_sync.py
│   │   └── document_parsing.py
│   ├── stores/
│   │   ├── onedrive/
│   │   │   ├── OneDriveInterface.py, OneDriveEnums.py, OneDriveProviderFactory.py
│   │   │   └── providers/MSALGraphProvider.py
│   │   └── llm/templates/clients/<client_id>/   # generated, gitignored — Bucket B/C (§3.5)
│   │       ├── prompt_templates.py
│   │       └── system_directives.py
│   ├── utils/
│   │   ├── dynamic_schema_loader.py   # stateless auto-discovery call site
│   │   └── template_file_writer.py    # the only code allowed to write templates/clients/*
│   └── models/
│       ├── BaseDataModel.py
│       ├── ChunkModel.py
│       ├── ClientConfigModel.py       # + get_client_id_by_admin_api_key
│       ├── SchemaRegistryModel.py     # get_or_register / update_bucket / set_mandatory_fields
│       ├── TokenCacheModel.py         # Fernet-encrypted, client_id-scoped
│       ├── StagingRowModel.py         # Bucket A only
│       ├── enums/BucketEnum.py
│       └── db_schemes/raylab/
│           ├── schemes/{raylab_base,knowledge_chunk,client_config,schema_registry,token_cache,staging_row}.py
│           ├── alembic.ini.example
│           └── alembic/{env.py, script.py.mako, versions/}
├── .gitignore
├── claude.md
└── README.md
```
