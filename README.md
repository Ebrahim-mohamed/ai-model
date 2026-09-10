# Raylab — Setup & Execution Guide

## 1. Windows Environment & Prerequisites Setup

1. **Install Git**
   Download and install [Git for Windows](https://git-scm.com/download/win), then verify in `cmd`:
   ```cmd
   git --version
   ```

2. **Clone the Repository**
   ```cmd
   git clone https://github.com/Menna-Harmas/Raylab.git Raylab_Project
   cd Raylab_Project
   ```

3. **Install Miniconda (Windows)**
   - Download from: `https://www.anaconda.com/docs/getting-started/miniconda/install`
   - During setup, **check the box** for "Add Miniconda to my PATH environment variable".
   - Verify in `cmd`:
     ```cmd
     conda --version
     ```

4. **Install Docker Desktop**
   - Download and install [Docker Desktop for Windows](https://www.docker.com/products/docker-desktop/).
   - In **Settings → General**, ensure **"Use the WSL 2 based engine"** is checked.
   - This is required infrastructure for Section 3 — Docker Desktop exposes the `docker`/`docker compose` CLI inside WSL2 automatically once Section 2 is complete.

5. **Create the Windows Conda Environment** *(optional — for VS Code IntelliSense/editing only; the project's actual runtime environment is the WSL one built in Section 2)*
   - Open the project folder in VS Code and launch the integrated terminal.
   - Run:
     ```cmd
     conda create -n raylab python=3.11 -y
     conda activate raylab
     ```

---

## 2. WSL & Ubuntu Setup

This is the project's **real execution environment** — every command in Section 3 runs from here.

1. **Install WSL & Ubuntu**
   Open **PowerShell as Administrator** and run:
   ```powershell
   wsl --install
   wsl --set-default-version 2
   wsl --version
   wsl --install Ubuntu
   ```

2. **Initial Ubuntu Configuration**
   - Launch **Ubuntu** from the Windows Start menu.
   - Complete setup by creating your `username` and `password`.
   - Update packages:
     ```bash
     sudo apt update
     ```

3. **Install Miniconda inside WSL (Ubuntu)**
   Open the WSL terminal in VS Code and run sequentially:
   ```bash
   cd ~
   curl -fsSL -o miniconda.sh https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh
   chmod +x miniconda.sh
   ./miniconda.sh -b -p $HOME/miniconda3
   ```
   Initialize and reload the shell:
   ```bash
   $HOME/miniconda3/bin/conda init bash
   source ~/.bashrc
   ```
   Anaconda's default channels require a one-time Terms of Service acceptance, or the very first `conda create` fails with `CondaToSNonInteractiveError`:
   ```bash
   conda tos accept --override-channels --channel https://repo.anaconda.com/pkgs/main
   conda tos accept --override-channels --channel https://repo.anaconda.com/pkgs/r
   ```

4. **Create & Activate the WSL Conda Environment**
   ```bash
   conda create -n raylab python=3.11 -y
   conda activate raylab
   ```
   Python 3.11 is required — `pgvector` and other pinned dependencies need Python ≥3.9, and 3.11 is the version this project is built and verified against.

---

## 3. Project Execution Pipeline

Run every command below from the **WSL Ubuntu terminal**, with the `raylab` conda environment active, unless stated otherwise.

### 3.1 Infrastructure containers (Postgres/pgvector, Redis, RabbitMQ)

```bash
cd /mnt/d/Raylab_Project/docker   # adjust the drive/path to wherever you cloned the repo
cp env/.env.example.postgres env/.env.postgres
cp env/.env.example.redis env/.env.redis
cp env/.env.example.rabbitmq env/.env.rabbitmq
# edit the three files above if you want non-default credentials — defaults work as-is

docker compose up -d pgvector redis rabbitmq
docker compose ps        # wait until all three show "healthy" before continuing
cd ..
```

This starts `raylab-pgvector` (host port `5433`), `raylab-redis` (host port `6380`), and `raylab-rabbitmq` (host port `5673`, management UI on `15673`).

### 3.2 Install Python dependencies (CPU-only `torch` first)

```bash
cd src
pip install --index-url https://download.pytorch.org/whl/cpu torch==2.13.0
pip install -r requirements.txt   # torch is already satisfied; installs everything else
```

`torch` **must** be installed from the CPU-only wheel index before the rest of `requirements.txt` — letting `pip` resolve it as a transitive dependency pulls the full CUDA build (multiple GB of `nvidia-*` wheels). If you have real GPU hardware you intend to use locally, install a matching CUDA build of `torch` yourself instead, before `pip install -r requirements.txt`.

### 3.3 Configure environment files

```bash
cp .env.example .env
# edit .env: set POSTGRES_* to match docker/env/.env.postgres, and set
# MSAL_CLIENT_ID / TOKEN_CACHE_ENCRYPTION_KEY (required for the OneDrive sync step)
```

### 3.4 Run database migrations

```bash
cd models/db_schemes/raylab
cp alembic.ini.example alembic.ini
# edit alembic.ini's sqlalchemy.url only if you changed the Postgres port/password/db name

alembic upgrade head
alembic current            # should print the current head revision id, marked (head)
cd ../../..
```

Verify the schema landed correctly:

```bash
docker exec -it raylab-pgvector psql -U postgres -d raylab -c '\dt'
```

### 3.5 Onboard a client

**Required before `POST /api/sync` or any chat request will work.** This performs the one-time MSAL Device Code Flow login and seeds `client_config` (drive ID, folder Item ID, admin API key) in one step:

```bash
cd scripts
python onboard_client.py \
  --client raylab \
  --drive-id <ONEDRIVE_DRIVE_ID> \
  --item-id '<ONEDRIVE_SHARED_FOLDER_ITEM_ID>' \
  --api-key raylab-admin-test-key
cd ..
```

Follow the printed device-login instructions to complete the sign-in.

### 3.6 Start the Celery worker

In its own terminal (same conda environment):

```bash
cd src
celery -A celery_app worker --queues=default,onedrive_sync,document_parsing,chunk_generation,embedding_shootout,embedding_generation,whatsapp_text --loglevel=info
```

Leave this running. Confirm the startup banner ends with `celery@<host> ready.`.

### 3.7 Start the FastAPI server

In a second terminal (same conda environment):

```bash
cd src
uvicorn main:app --reload --port 8000
```

Smoke-test in a third terminal:

```bash
curl http://localhost:8000/api/
```

Expect `{"app_name":"Raylab","app_version":"0.1"}`.

### 3.8 Deploy the LLM generation backend (external GPU host)

The FastAPI app calls out to an OpenAI-compatible vLLM endpoint for generation/routing — it does **not** run the model itself. Launch it on a GPU host (Colab, rented GPU, on-prem):

```bash
vllm serve MBZUAI-Paris/Nile-Chat-12B \
  --dtype bfloat16 --max-model-len 8192 --gpu-memory-utilization 0.85 \
  --port 8001 --served-model-name nile-chat-12b-base \
  --enable-lora --max-lora-rank 16 \
  --lora-modules mode-a-lora=mennaharmas/raylab-nilechat-12b-v2-lora
```

Ready-made launch notebooks (vLLM install, GPU check, tunnel setup) are under `src/run_model/` — use `serve_consolidated_multi_lora.ipynb` for the current single-server setup (base model + Mode A LoRA adapter on one port).

Point `src/.env` at the reachable endpoint (a Cloudflare/ngrok tunnel URL if running on Colab), then restart `uvicorn`:

```ini
GENERATION_BACKEND="NILE_CHAT_12B"
GENERATION_BASE_URL="<tunnel URL for the host running vLLM>"
GENERATION_MODEL_NAME="mode-a-lora"

QUERY_ROUTER_BACKEND="NILE_CHAT_12B_BASE"
QUERY_ROUTER_BASE_URL="<same or separate tunnel URL>"
QUERY_ROUTER_MODEL_NAME="nile-chat-12b-base"
```

### 3.9 Verify end-to-end

```bash
curl -X POST http://localhost:8000/api/whatsapp/chat \
  -H "X-Admin-Api-Key: raylab-admin-test-key" \
  -H "Content-Type: application/json" \
  -d '{"session_id": "11111111-1111-1111-1111-111111111111", "message": "أهلا"}'
```

**Available endpoints:**

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/` | Health check |
| POST | `/api/sync` | Trigger a OneDrive sync for a client |
| GET | `/api/sync/{task_id}/status` | Poll sync task status |
| POST | `/api/retrieve` | Raw hybrid-retrieval debug endpoint |
| POST | `/api/whatsapp/chat` | Chat pipeline (intent routing + reply generation) |
| GET | `/api/analytics/dashboard` | Analytics dashboard KPIs |

All authenticated routes take header `X-Admin-Api-Key: <the key set during onboarding>`.

---

## Daily Startup (after the first-time setup above is done)

```bash
# 1. infra containers
cd /mnt/d/Raylab_Project/docker && docker compose up -d pgvector redis rabbitmq && docker compose ps

# 2. conda env
conda activate raylab

# 3. celery worker (own terminal)
cd /mnt/d/Raylab_Project/src
celery -A celery_app worker --queues=default,onedrive_sync,document_parsing,chunk_generation,embedding_shootout,embedding_generation,whatsapp_text --loglevel=info

# 4. fastapi server (second terminal)
cd /mnt/d/Raylab_Project/src
uvicorn main:app --reload --port 8000

# 5. smoke test (third terminal)
curl http://localhost:8000/api/
```

If a request fails with `Connection reset by peer` against Postgres/Redis after a Windows sleep/wake cycle:

```bash
docker restart raylab-pgvector raylab-redis raylab-rabbitmq
```

then restart the Celery worker and FastAPI server (steps 3–4 above).
