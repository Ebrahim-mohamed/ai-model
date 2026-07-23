from cryptography.fernet import Fernet
from sqlalchemy.future import select

from .BaseDataModel import BaseDataModel
from .db_schemes.raylab.schemes import TokenCache


class TokenCacheModel(BaseDataModel):
    """Repository for token_cache. Every method requires client_id — no
    exceptions. Owns all encryption/decryption of the stored blob; callers
    (MSALGraphProvider) only ever see the decrypted MSAL
    SerializableTokenCache JSON string, never the ciphertext or the key."""

    def __init__(self, db_client: object):
        super().__init__(db_client=db_client)
        self._fernet = Fernet(self.app_settings.TOKEN_CACHE_ENCRYPTION_KEY)

    @classmethod
    async def create_instance(cls, db_client: object):
        return cls(db_client)

    async def get_token_cache(self, client_id: str) -> str | None:
        """Returns the decrypted MSAL SerializableTokenCache payload for
        this client, or None if this client has never been onboarded
        (no Device Code Flow has ever been run for them)."""
        async with self.db_client() as session:
            stmt = select(TokenCache).where(TokenCache.client_id == client_id)
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()

        if row is None:
            return None
        return self._fernet.decrypt(row.encrypted_cache).decode("utf-8")

    async def save_token_cache(self, client_id: str, serialized_cache: str) -> None:
        """Upsert — the one-time Device Code Flow inserts, every silent
        token refresh afterward updates the same row in place."""
        encrypted = self._fernet.encrypt(serialized_cache.encode("utf-8"))

        async with self.db_client() as session:
            async with session.begin():
                stmt = select(TokenCache).where(TokenCache.client_id == client_id)
                result = await session.execute(stmt)
                row = result.scalar_one_or_none()

                if row is None:
                    session.add(TokenCache(client_id=client_id, encrypted_cache=encrypted))
                else:
                    row.encrypted_cache = encrypted

            await session.commit()
