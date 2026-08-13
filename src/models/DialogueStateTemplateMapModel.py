from sqlalchemy.future import select

from .BaseDataModel import BaseDataModel
from .db_schemes.raylab.schemes import DialogueStateTemplateMap


class DialogueStateTemplateMapModel(BaseDataModel):
    """Repository for dialogue_state_template_map — the data TextReplyController
    reads to decide whether the current dialogue state fires Mode B
    (verbatim Bucket B/C template) instead of Mode A. Every method
    requires client_id, no exceptions."""

    def __init__(self, db_client: object):
        super().__init__(db_client=db_client)

    @classmethod
    async def create_instance(cls, db_client: object):
        return cls(db_client)

    async def get_template_for_state(self, client_id: str, dialogue_state: str) -> DialogueStateTemplateMap | None:
        async with self.db_client() as session:
            stmt = select(DialogueStateTemplateMap).where(
                DialogueStateTemplateMap.client_id == client_id,
                DialogueStateTemplateMap.dialogue_state == dialogue_state,
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def upsert_mapping(self, client_id: str, dialogue_state: str, bucket: str, template_id: str) -> DialogueStateTemplateMap:
        """Admin-facing upsert — how a new scripted moment is added
        (a row insert, never a new `if` branch in TextReplyController)."""
        async with self.db_client() as session:
            async with session.begin():
                stmt = select(DialogueStateTemplateMap).where(
                    DialogueStateTemplateMap.client_id == client_id,
                    DialogueStateTemplateMap.dialogue_state == dialogue_state,
                )
                result = await session.execute(stmt)
                mapping = result.scalar_one_or_none()

                if mapping is None:
                    mapping = DialogueStateTemplateMap(
                        client_id=client_id, dialogue_state=dialogue_state,
                        bucket=bucket, template_id=template_id,
                    )
                    session.add(mapping)
                else:
                    mapping.bucket = bucket
                    mapping.template_id = template_id

            await session.commit()
            await session.refresh(mapping)
        return mapping
