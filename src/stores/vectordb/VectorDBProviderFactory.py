from .VectorDBEnums import VectorDBEnums
from .providers.PGVectorProvider import PGVectorProvider


class VectorDBProviderFactory:
    """The only code allowed to map a VECTOR_DB_BACKEND config string to a
    concrete adapter — identical pattern to OneDriveProviderFactory. A new
    vector DB (Pinecone, Qdrant, ...) is one new providers/ file plus one
    branch here; nothing in ChunkModel or any controller changes."""

    def __init__(self, config, db_client):
        self.config = config
        self.db_client = db_client

    def create(self, provider: str):
        if provider == VectorDBEnums.PGVECTOR.value:
            return PGVectorProvider(db_client=self.db_client)

        return None
