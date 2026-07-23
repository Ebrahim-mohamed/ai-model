from sqlalchemy import Column, String, LargeBinary, DateTime, func

from .raylab_base import SQLAlchemyBase


class TokenCache(SQLAlchemyBase):
    """Each client's encrypted MSAL token-cache blob, keyed strictly by
    client_id. `encrypted_cache` is Fernet ciphertext (TOKEN_CACHE_ENCRYPTION_KEY)
    — never the plaintext MSAL SerializableTokenCache JSON. Encryption and
    decryption happen only in TokenCacheModel, never here or in a provider.
    """

    __tablename__ = "token_cache"

    client_id = Column(String, primary_key=True)
    encrypted_cache = Column(LargeBinary, nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
