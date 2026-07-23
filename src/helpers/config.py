from typing import List

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Single source of truth for all environment/config values.

    Step 1 added the Postgres fields. Step 2 added the embedding backend
    literal set (Step 8 selects one) and the platform-wide
    boilerplate-threshold fallback. Step 3 wires up MSAL (now genuinely
    consumed, so it's required rather than optional) and the OneDrive
    provider selection — additive; nothing prior is reworked.
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

    # Platform-wide fallback only. The value that actually governs a given
    # client's boilerplate detection is client_config.boilerplate_threshold
    # — this constant is never read in place of that per-tenant row.
    DEFAULT_BOILERPLATE_THRESHOLD: float = 0.9

    class Config:
        env_file = ".env"


def get_settings() -> Settings:
    return Settings()
