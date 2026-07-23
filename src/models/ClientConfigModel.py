from sqlalchemy.future import select

from .BaseDataModel import BaseDataModel
from .db_schemes.raylab.schemes import ClientConfig


class ClientConfigModel(BaseDataModel):
    """Repository for client_config — every method requires client_id, no exceptions."""

    def __init__(self, db_client: object):
        super().__init__(db_client=db_client)

    @classmethod
    async def create_instance(cls, db_client: object):
        return cls(db_client)

    async def create_client_config(self, client_id: str, **fields) -> ClientConfig:
        client_config = ClientConfig(client_id=client_id, **fields)
        async with self.db_client() as session:
            async with session.begin():
                session.add(client_config)
            await session.commit()
            await session.refresh(client_config)
        return client_config

    async def get_client_config(self, client_id: str) -> ClientConfig | None:
        async with self.db_client() as session:
            stmt = select(ClientConfig).where(ClientConfig.client_id == client_id)
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def get_client_id_by_admin_api_key(self, admin_api_key: str) -> str | None:
        """The one deliberate exception to 'every method requires client_id':
        this method's entire purpose is resolving client_id FROM a
        credential, for the sync route's auth dependency. client_id must
        never be a client-supplied field (Implementation Plan Step 4)."""
        async with self.db_client() as session:
            stmt = select(ClientConfig.client_id).where(ClientConfig.admin_api_key == admin_api_key)
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def upsert_client_config(self, client_id: str, **fields) -> ClientConfig:
        async with self.db_client() as session:
            async with session.begin():
                stmt = select(ClientConfig).where(ClientConfig.client_id == client_id)
                result = await session.execute(stmt)
                client_config = result.scalar_one_or_none()

                if client_config is None:
                    client_config = ClientConfig(client_id=client_id, **fields)
                    session.add(client_config)
                else:
                    for key, value in fields.items():
                        setattr(client_config, key, value)

            await session.commit()
            await session.refresh(client_config)
        return client_config
