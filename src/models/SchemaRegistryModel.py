from sqlalchemy.future import select

from .BaseDataModel import BaseDataModel
from .db_schemes.raylab.schemes import SchemaRegistry
from .enums.BucketEnum import BucketEnum


class SchemaRegistryModel(BaseDataModel):
    """Repository for schema_registry. Every method requires client_id — no exceptions."""

    def __init__(self, db_client: object):
        super().__init__(db_client=db_client)

    @classmethod
    async def create_instance(cls, db_client: object):
        return cls(db_client)

    async def get_schema(self, client_id: str, sheet_name: str) -> SchemaRegistry | None:
        async with self.db_client() as session:
            stmt = select(SchemaRegistry).where(
                SchemaRegistry.client_id == client_id,
                SchemaRegistry.sheet_name == sheet_name,
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def list_sheets(self, client_id: str) -> list[SchemaRegistry]:
        async with self.db_client() as session:
            stmt = select(SchemaRegistry).where(SchemaRegistry.client_id == client_id)
            result = await session.execute(stmt)
            return result.scalars().all()

    async def get_or_register(
        self,
        client_id: str,
        sheet_name: str,
        discovered_columns: list[str],
        default_bucket: BucketEnum = BucketEnum.VECTOR_DB,
    ) -> SchemaRegistry:
        """The auto-discovery entry point (claude.md §3.2).

        Unregistered sheet -> inserted on the spot, defaulted to
        `default_bucket` with no mandatory fields. Already-registered sheet
        -> its bucket and mandatory_fields are left untouched (that's the
        one deliberate, reviewed step an engineer takes later, via
        `update_bucket`/`set_mandatory_fields`), but its column list is
        refreshed to whatever was just discovered, so column drift (added
        or removed fields) is picked up on every call, registered or not.
        Calling this twice with identical columns is a no-op write and
        always returns the same row — never a duplicate.
        """
        async with self.db_client() as session:
            async with session.begin():
                stmt = select(SchemaRegistry).where(
                    SchemaRegistry.client_id == client_id,
                    SchemaRegistry.sheet_name == sheet_name,
                )
                result = await session.execute(stmt)
                schema_row = result.scalar_one_or_none()

                if schema_row is None:
                    schema_row = SchemaRegistry(
                        client_id=client_id,
                        sheet_name=sheet_name,
                        columns=list(discovered_columns),
                        bucket=default_bucket.value,
                        mandatory_fields=[],
                    )
                    session.add(schema_row)
                elif schema_row.columns != list(discovered_columns):
                    schema_row.columns = list(discovered_columns)

            await session.commit()
            await session.refresh(schema_row)
        return schema_row

    async def update_bucket(self, client_id: str, sheet_name: str, bucket: BucketEnum) -> SchemaRegistry | None:
        """The one deliberate, reviewed reclassification step (claude.md
        §3.2 point 5) — never called by the auto-discovery path itself."""
        async with self.db_client() as session:
            async with session.begin():
                stmt = select(SchemaRegistry).where(
                    SchemaRegistry.client_id == client_id,
                    SchemaRegistry.sheet_name == sheet_name,
                )
                result = await session.execute(stmt)
                schema_row = result.scalar_one_or_none()
                if schema_row is not None:
                    schema_row.bucket = bucket.value

            if schema_row is not None:
                await session.commit()
                await session.refresh(schema_row)
        return schema_row

    async def set_mandatory_fields(self, client_id: str, sheet_name: str, mandatory_fields: list[str]) -> SchemaRegistry | None:
        """Also a deliberate, reviewed step — an auto-registered sheet has
        no mandatory fields until an engineer adds them here."""
        async with self.db_client() as session:
            async with session.begin():
                stmt = select(SchemaRegistry).where(
                    SchemaRegistry.client_id == client_id,
                    SchemaRegistry.sheet_name == sheet_name,
                )
                result = await session.execute(stmt)
                schema_row = result.scalar_one_or_none()
                if schema_row is not None:
                    schema_row.mandatory_fields = list(mandatory_fields)

            if schema_row is not None:
                await session.commit()
                await session.refresh(schema_row)
        return schema_row
