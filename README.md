# Raylab

Multi-tenant RAG backend (Section 2: OneDrive Integration). See `claude.md` for the full
architectural constitution and `D:\project\Implementation Plan — Section 2 The Multi-Tenant.md`
for the build order this project follows step by step.

## Status

**Step 2 of 9 complete: Core Configuration & Dynamic Schema Registry.**

Implemented so far:
- **Step 1** — `knowledge_chunks` (pgvector store, `client_id NOT NULL`, `embedding VECTOR(1024)`), `client_config` (tenant registry), `ChunkModel`, `ClientConfigModel`, `BucketEnum`.
- **Step 2** — `helpers/config.py` extended (MSAL scaffolding, `EMBEDDING_BACKEND_LITERAL`, `DEFAULT_BOILERPLATE_THRESHOLD`); `schema_registry` table + `SchemaRegistryModel` (auto-discovery/auto-registration, upsert-on-discovery, zero manual DB entry); stateless `utils/dynamic_schema_loader.py`.
- Two chained Alembic migrations: `7ec06e4ca8ba` (Step 1) → `48d3c854b890` (Step 2).

Not built yet (later steps): OneDrive/MSAL store, sync endpoint, chunking engine, vector storage layer, embedding shootout, retrieval endpoint. Do not assume any of these exist.

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

## Environment variables

| Variable | File | Purpose |
|---|---|---|
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | `docker/env/.env.postgres` | Postgres container credentials |
| `APP_NAME`, `APP_VERSION` | `src/.env` | App identity (used by `helpers/config.py`) |
| `POSTGRES_USERNAME` / `POSTGRES_PASSWORD` / `POSTGRES_HOST` / `POSTGRES_PORT` / `POSTGRES_MAIN_DATABASE` | `src/.env` | App-side Postgres connection (host: `localhost`, port: `5433`) |
| `MSAL_CLIENT_ID` / `MSAL_TOKEN_CACHE_PATH` | `src/.env` | Reserved for Step 3 (OneDrive/MSAL store) — optional, unused until then |
| `EMBEDDING_BACKEND_LITERAL` | `src/.env` | Self-documenting list of implemented embedding backends (Step 8 selects one) |
| `DEFAULT_BOILERPLATE_THRESHOLD` | `src/.env` | Platform-wide fallback only — per-client value always comes from `client_config.boilerplate_threshold` |

## Project layout

See `claude.md` §1.1 for the full target directory tree. Only the Steps 1–2 subset exists today:

```
Raylab_Project/
├── docker/
│   ├── docker-compose.yml      # pgvector service only, for now
│   └── env/.env.example.postgres
├── src/
│   ├── requirements.txt
│   ├── .env.example
│   ├── helpers/config.py       # Postgres + MSAL scaffolding + embedding/threshold literals
│   ├── utils/
│   │   └── dynamic_schema_loader.py   # stateless auto-discovery call site (Step 5 wires it up)
│   └── models/
│       ├── BaseDataModel.py
│       ├── ChunkModel.py
│       ├── ClientConfigModel.py
│       ├── SchemaRegistryModel.py     # get_or_register / update_bucket / set_mandatory_fields
│       ├── enums/BucketEnum.py
│       └── db_schemes/raylab/
│           ├── schemes/{raylab_base,knowledge_chunk,client_config,schema_registry}.py
│           ├── alembic.ini.example
│           └── alembic/{env.py, script.py.mako, versions/}
├── .gitignore
├── claude.md
└── README.md
```
