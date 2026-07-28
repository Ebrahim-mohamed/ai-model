from sqlalchemy.future import select
from sqlalchemy import delete

from .BaseDataModel import BaseDataModel
from .db_schemes.raylab.schemes import EvaluationQuery, ShootoutResult


class EvaluationQueryModel(BaseDataModel):
    """Repository for the Step 8 benchmark query set and its scored
    results. Every method requires client_id — no exceptions."""

    def __init__(self, db_client: object):
        super().__init__(db_client=db_client)

    @classmethod
    async def create_instance(cls, db_client: object):
        return cls(db_client)

    async def seed_queries(self, client_id: str, queries: list[dict]) -> int:
        """queries: [{"query_text": ..., "expected_chunk_id": ...}, ...].
        Delete-and-reinsert scoped to client_id (claude.md §2.3) — the
        benchmark set is fully replaceable, never incrementally patched."""
        async with self.db_client() as session:
            async with session.begin():
                await session.execute(
                    delete(EvaluationQuery).where(EvaluationQuery.client_id == client_id)
                )
                session.add_all([
                    EvaluationQuery(client_id=client_id, **query)
                    for query in queries
                ])
            await session.commit()
        return len(queries)

    async def get_queries(self, client_id: str) -> list[EvaluationQuery]:
        async with self.db_client() as session:
            stmt = select(EvaluationQuery).where(EvaluationQuery.client_id == client_id)
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def record_result(
        self,
        client_id: str,
        model_name: str,
        query_count: int,
        top_k: int,
        top_k_accuracy: float | None = None,
        error_message: str | None = None,
    ) -> ShootoutResult:
        result_row = ShootoutResult(
            client_id=client_id,
            model_name=model_name,
            top_k_accuracy=top_k_accuracy,
            query_count=query_count,
            top_k=top_k,
            error_message=error_message,
        )
        async with self.db_client() as session:
            async with session.begin():
                session.add(result_row)
            await session.commit()
            await session.refresh(result_row)
        return result_row

    async def get_results(self, client_id: str) -> list[ShootoutResult]:
        async with self.db_client() as session:
            stmt = select(ShootoutResult).where(ShootoutResult.client_id == client_id)
            result = await session.execute(stmt)
            return list(result.scalars().all())
