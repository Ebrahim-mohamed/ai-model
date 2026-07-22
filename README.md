# Raylab

Multi-tenant RAG backend (Section 2: OneDrive Integration). See `claude.md` for the full
architectural constitution and `D:\project\Implementation Plan — Section 2 The Multi-Tenant.md`
for the build order this project follows step by step.

## Status

**Step 1 of 9 complete: Multi-Tenant Database Schema & Client Registry.**

Implemented so far:
- `knowledge_chunks` — the pgvector store (`client_id NOT NULL`, `embedding VECTOR(1024)`, `metadata JSONB`).
- `client_config` — the tenant registry (`client_id` PK, `allowed_metadata_keys[]`, `boilerplate_threshold`, `onedrive_item_id`, `embedding_backend_override`).
- Repositories: `ChunkModel`, `ClientConfigModel` (every method requires `client_id`).
- `BucketEnum` (`VECTOR_DB` / `PROMPT_TEMPLATE` / `SYSTEM_DIRECTIVE`).
- One Alembic migration creating both tables plus `idx_chunks_client` (btree), `idx_chunks_metadata` (GIN), `idx_chunks_embedding_hnsw` (HNSW, cosine).

Not built yet (later steps): `helpers/config.py`'s non-Postgres fields, schema registry / auto-discovery, OneDrive/MSAL store, sync endpoint, chunking engine, retrieval endpoint. Do not assume any of these exist.

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
`pydantic-settings` (Step 1 scope only — later steps append their own dependencies here).

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

## Environment variables

| Variable | File | Purpose |
|---|---|---|
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | `docker/env/.env.postgres` | Postgres container credentials |
| `APP_NAME`, `APP_VERSION` | `src/.env` | App identity (used by `helpers/config.py`) |
| `POSTGRES_USERNAME` / `POSTGRES_PASSWORD` / `POSTGRES_HOST` / `POSTGRES_PORT` / `POSTGRES_MAIN_DATABASE` | `src/.env` | App-side Postgres connection (host: `localhost`, port: `5433`) |

## Project layout

See `claude.md` §1.1 for the full target directory tree. Only the Step 1 subset exists today:

```
Raylab_Project/
├── docker/
│   ├── docker-compose.yml      # pgvector service only, for now
│   └── env/.env.example.postgres
├── src/
│   ├── requirements.txt
│   ├── .env.example
│   ├── helpers/config.py       # Postgres fields only — Step 2 extends this
│   └── models/
│       ├── BaseDataModel.py
│       ├── ChunkModel.py
│       ├── ClientConfigModel.py
│       ├── enums/BucketEnum.py
│       └── db_schemes/raylab/
│           ├── schemes/{raylab_base,knowledge_chunk,client_config}.py
│           ├── alembic.ini.example
│           └── alembic/{env.py, script.py.mako, versions/}
├── .gitignore
├── claude.md
└── README.md
```
