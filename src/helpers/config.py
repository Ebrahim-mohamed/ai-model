from typing import List

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Single source of truth for all environment/config values.

    Step 1 added the Postgres fields. Step 2 added the embedding backend
    literal set (Step 8 selects one). Step 3 wired up MSAL (now genuinely
    consumed, so it's required rather than optional) and the OneDrive
    provider selection. Step 4 adds Celery broker/backend config — additive;
    nothing prior is reworked.
    """

    APP_NAME: str
    APP_VERSION: str

    POSTGRES_USERNAME: str
    POSTGRES_PASSWORD: str
    POSTGRES_HOST: str
    POSTGRES_PORT: int
    POSTGRES_MAIN_DATABASE: str

    # MSAL Device Code Flow (Step 3 — OneDrive/MSAL store). Public client
    # app ID from the Azure AD app registration (see README's Step 3
    # section for how to create one).
    MSAL_CLIENT_ID: str

    # Fernet key encrypting each client's token-cache blob at rest in
    # token_cache.encrypted_cache — TokenCacheModel never stores plaintext.
    # Generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    TOKEN_CACHE_ENCRYPTION_KEY: str

    # Config-driven provider selection, identical pattern to
    # VECTOR_DB_BACKEND/GENERATION_BACKEND in response.md — swapping the
    # OneDrive provider (e.g. a future SharePoint adapter) is a .env edit.
    ONEDRIVE_AUTH_BACKEND_LITERAL: List[str] = ["MSAL_GRAPH"]
    ONEDRIVE_AUTH_BACKEND: str = "MSAL_GRAPH"

    # Self-documenting set of implemented embedding backends (Step 8's
    # shootout candidates) — same pattern as response.md's
    # GENERATION_MODEL_ID_LITERAL: a literal list, not an enforced enum,
    # since it only documents which providers exist in stores/llm/providers/.
    EMBEDDING_BACKEND_LITERAL: List[str] = ["BGE_M3", "SWAN_LARGE"]

    # Config-driven embedding provider selection (Step 8) — identical
    # pattern to VECTOR_DB_BACKEND/ONEDRIVE_AUTH_BACKEND. This is the one
    # flag EmbeddingShootoutController's winner promotion flips — no
    # controller or task code changes when it does.
    EMBEDDING_BACKEND: str = "BGE_M3"

    # Optional HuggingFace token for gated model repos (Step 8's
    # Swan-Large candidate needs one; BGE-M3 is ungated and ignores this).
    HF_TOKEN: str | None = None

    # Config-driven vector DB provider selection (Step 7) — identical
    # pattern to ONEDRIVE_AUTH_BACKEND. Swapping pgvector for a future
    # hosted vector DB is a .env edit plus one new providers/ file, never
    # a code change in ChunkModel or any controller.
    VECTOR_DB_BACKEND_LITERAL: List[str] = ["PGVECTOR"]
    VECTOR_DB_BACKEND: str = "PGVECTOR"

    # Config-driven re-ranker provider selection (Step 9) — identical
    # pattern to VECTOR_DB_BACKEND. A new re-ranking vendor is one new
    # providers/ file plus one branch in RerankerProviderFactory.
    # 2026-09-02: BGE_RERANKER_V2_M3 added as a togglable A/B candidate
    # (real evidence — a lexical-overlap retrieval failure on PET-CT
    # queries) — switch RERANKER_BACKEND in .env to test it; default
    # stays CROSS_ENCODER (the currently-deployed, currently-proven
    # model) unless/until real traffic shows the swap is worth its extra
    # latency (see BGERerankerV2M3Provider's own docstring).
    RERANKER_BACKEND_LITERAL: List[str] = ["CROSS_ENCODER", "BGE_RERANKER_V2_M3"]
    RERANKER_BACKEND: str = "BGE_RERANKER_V2_M3"

    # Celery — task queue config for the on-demand sync trigger (Step 4).
    # No beat/schedule fields exist here on purpose: OneDrive sync is
    # human-initiated only, never periodic (claude.md §2.3).
    CELERY_BROKER_URL: str
    CELERY_RESULT_BACKEND: str
    CELERY_TASK_SERIALIZER: str = "json"
    CELERY_TASK_TIME_LIMIT: int = 600
    CELERY_TASK_ACKS_LATE: bool = True
    CELERY_WORKER_CONCURRENCY: int = 2

    # Section 3 Step 1 — config-driven generation-model provider selection,
    # identical pattern to EMBEDDING_BACKEND/VECTOR_DB_BACKEND. QwenProvider
    # is an OpenAI-compatible HTTP client; GENERATION_BASE_URL points at
    # wherever that endpoint is actually served (a local vLLM instance, a
    # rented GPU box) — the app code never knows or cares which, only that
    # the URL is reachable (claude.md §6.3 Ports & Adapters).
    # models => nile-chat-12b, falcon-h1-34b
    GENERATION_BACKEND_LITERAL: List[str] = ["NILE_CHAT_12B"]
    GENERATION_BACKEND: str = "NILE_CHAT_12B"
    GENERATION_BASE_URL: str
    GENERATION_MODEL_NAME: str = "raylab-nilechat-finetuned"
    # Raised 30 -> 120 during Phase 0's bake-off (see README/.env.example)
    # — real traffic against a --enforce-eager, tunneled 34B candidate
    # showed 5/67 turns exceeding 30s purely on inference latency.
    GENERATION_REQUEST_TIMEOUT_SECONDS: int = 120

    # Dual-model architecture, 2026-08-30 (see README's own history for
    # the real production evidence behind this): a SEPARATE, lightweight
    # sidecar dedicated to IntentRoutingController's classify-and-rewrite
    # call only — GENERATION_* above is reserved for Mode A grounded
    # reply generation exclusively from this point forward. This is not
    # the same config-driven-backend-selection pattern as GENERATION_*
    # (swap which model plays the SAME role) — it's a second, genuinely
    # independent role, on its own store (stores/query_router/), so it
    # gets its own base_url/model_name/timeout rather than sharing
    # GENERATION_*'s.
    # 2026-09-01: NILE_CHAT_4B retired entirely (real evidence — four
    # separate prompt-engineering attempts all failed the same generic-
    # reference anaphora-resolution pattern; see QueryRouterInterface's
    # own docstring for the full history) and replaced by
    # NILE_CHAT_12B_BASE — the raw, unadapted MBZUAI-Paris/Nile-Chat-12B
    # checkpoint Mode A's own fine-tuned GENERATION_BACKEND was trained
    # FROM, used here in its general-purpose form.
    QUERY_ROUTER_BACKEND_LITERAL: List[str] = ["NILE_CHAT_12B_BASE"]
    QUERY_ROUTER_BACKEND: str = "NILE_CHAT_12B_BASE"
    QUERY_ROUTER_BASE_URL: str
    QUERY_ROUTER_MODEL_NAME: str = "nile-chat-12b-base"
    # Deliberately a tighter default than GENERATION_REQUEST_TIMEOUT_SECONDS
    # (120s) — this call runs in front of every single inbound turn, before
    # any retrieval, and its own output is always small (150-180 max_tokens
    # in NileChat12BBaseProvider, vs. up to 2048 for a broad Mode A reply),
    # so a slow response here should fail fast into its own safe fallback
    # rather than stall the whole turn as long as Mode A's own generation
    # budget allows. Nudged 15s -> 20s (2026-09-01) purely for the size
    # change — this sidecar now runs the same 12B-class model as Mode A
    # itself, not a smaller 4B one, so per-token latency is real even
    # though total output length is still small. Still a starting point,
    # not a calibrated value — revisit with real measured latency once
    # this deployment has traffic behind it.
    QUERY_ROUTER_REQUEST_TIMEOUT_SECONDS: int = 20

    # Section 3 Step 1 — Tier 1 (Redis) of the two-tier chat-history
    # architecture: active session state (dialogue stage, collected slots,
    # rolling message window), separate from Celery's own broker/result
    # Redis usage above — a different concern, kept on its own config
    # field even if it happens to point at the same Redis instance.
    SESSION_REDIS_URL: str
    SESSION_TTL_SECONDS: int = 86400
    SESSION_HISTORY_WINDOW: int = 6

    # Step-by-step RAG pipeline tracing (breadth classification, field
    # selection, final CONTEXT construction — see helpers/logging_config.py).
    # RAG_LOG_LEVEL is deliberately env-driven rather than a code constant:
    # flipping DEBUG on for a live debugging session, or back to INFO for
    # normal operation, should never require a redeploy.
    RAG_LOG_FILE_PATH: str = "rag_execution.log"
    RAG_LOG_LEVEL: str = "INFO"

    class Config:
        env_file = ".env"


def get_settings() -> Settings:
    return Settings()
