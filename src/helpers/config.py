from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Single source of truth for all environment/config values.

    Only Postgres fields exist here for now — Step 1 needs nothing else.
    Step 2 extends this class (MSAL, schema-registry, embedding backend
    literals, thresholds) additively; nothing here is removed or reworked.
    """

    APP_NAME: str
    APP_VERSION: str

    POSTGRES_USERNAME: str
    POSTGRES_PASSWORD: str
    POSTGRES_HOST: str
    POSTGRES_PORT: int
    POSTGRES_MAIN_DATABASE: str

    class Config:
        env_file = ".env"


def get_settings() -> Settings:
    return Settings()
