from typing import List

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Single source of truth for all environment/config values.

    Step 1 added the Postgres fields. Step 2 adds MSAL scaffolding (Step 3
    wires up its actual use), the embedding backend literal set (Step 8
    selects one), and the platform-wide boilerplate-threshold fallback —
    additive; nothing prior is removed or reworked.
    """

    APP_NAME: str
    APP_VERSION: str

    POSTGRES_USERNAME: str
    POSTGRES_PASSWORD: str
    POSTGRES_HOST: str
    POSTGRES_PORT: int
    POSTGRES_MAIN_DATABASE: str

    # MSAL Device Code Flow (Step 3 — OneDrive/MSAL store). Optional for now:
    # nothing consumes these until Step 3 exists, so Settings() must still
    # instantiate cleanly for anyone running Steps 1-2 alone.
    MSAL_CLIENT_ID: str = None
    MSAL_TOKEN_CACHE_PATH: str = None

    # Self-documenting set of implemented embedding backends (Step 8's
    # shootout candidates) — same pattern as response.md's
    # GENERATION_MODEL_ID_LITERAL: a literal list, not an enforced enum,
    # since it only documents which providers exist in stores/llm/providers/.
    EMBEDDING_BACKEND_LITERAL: List[str] = ["BGE_M3", "SWAN_LARGE"]

    # Platform-wide fallback only. The value that actually governs a given
    # client's boilerplate detection is client_config.boilerplate_threshold
    # — this constant is never read in place of that per-tenant row.
    DEFAULT_BOILERPLATE_THRESHOLD: float = 0.9

    class Config:
        env_file = ".env"


def get_settings() -> Settings:
    return Settings()
