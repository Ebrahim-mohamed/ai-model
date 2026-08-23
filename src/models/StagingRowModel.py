from sqlalchemy.future import select
from sqlalchemy import delete, func

from .BaseDataModel import BaseDataModel
from .db_schemes.raylab.schemes import StagingRow


class StagingRowModel(BaseDataModel):
    """Repository for staging_rows. Every method requires client_id, no
    exceptions."""

    def __init__(self, db_client: object):
        super().__init__(db_client=db_client)

    @classmethod
    async def create_instance(cls, db_client: object):
        return cls(db_client)

    async def insert_many_rows(self, client_id: str, rows: list[dict], source_file: str = None, batch_size: int = 200) -> int:
        """Each item in `rows` is {"sheet_name": ..., "row_data": ...}."""
        staging_rows = [
            StagingRow(
                client_id=client_id,
                sheet_name=row["sheet_name"],
                row_data=row["row_data"],
                source_file=source_file,
            )
            for row in rows
        ]

        async with self.db_client() as session:
            async with session.begin():
                for i in range(0, len(staging_rows), batch_size):
                    session.add_all(staging_rows[i:i + batch_size])
            await session.commit()
        return len(staging_rows)

    async def get_rows(self, client_id: str, sheet_name: str = None, source_file: str = None) -> list[StagingRow]:
        async with self.db_client() as session:
            stmt = select(StagingRow).where(StagingRow.client_id == client_id)
            if sheet_name is not None:
                stmt = stmt.where(StagingRow.sheet_name == sheet_name)
            if source_file is not None:
                stmt = stmt.where(StagingRow.source_file == source_file)
            result = await session.execute(stmt)
            return result.scalars().all()

    async def delete_rows_by_source_file(self, client_id: str, source_file: str) -> int:
        """The delete side of delete-and-reinsert (claude.md §2.3) — this
        sync's staging rows fully replace the previous sync's for this
        client_id/source_file, never an incremental patch."""
        async with self.db_client() as session:
            stmt = delete(StagingRow).where(
                StagingRow.client_id == client_id,
                StagingRow.source_file == source_file,
            )
            result = await session.execute(stmt)
            await session.commit()
        return result.rowcount

    async def get_distinct_source_files(self, client_id: str) -> list[str]:
        """Every distinct source_file currently represented in
        staging_rows for this client — the staging-table counterpart of
        ChunkModel.get_distinct_source_files, used for the same full-mirror
        stale-file diff (tasks/onedrive_sync.py)."""
        async with self.db_client() as session:
            stmt = (
                select(StagingRow.source_file)
                .where(
                    StagingRow.client_id == client_id,
                    StagingRow.source_file.isnot(None),
                )
                .distinct()
            )
            result = await session.execute(stmt)
            return [row[0] for row in result.all()]

    async def get_total_rows_count(self, client_id: str) -> int:
        async with self.db_client() as session:
            stmt = select(func.count(StagingRow.id)).where(StagingRow.client_id == client_id)
            result = await session.execute(stmt)
            return result.scalar_one()
