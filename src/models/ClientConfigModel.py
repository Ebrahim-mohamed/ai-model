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
