from .BaseDataModel import BaseDataModel
from .db_schemes.raylab.schemes import HumanHandoffQueue


class HumanHandoffQueueModel(BaseDataModel):
    """Repository for human_handoff_queue. Every method requires
    client_id, no exceptions. Minimal by design (Implementation Plan's
    post-fine-tuning Step 8): the one write path `ReplyVerificationController`
    needs, and nothing a caller doesn't yet exist for — a resolve/list-open
    surface for an actual human-agent dashboard is a later, separately-scoped
    step, not pre-built here."""

    def __init__(self, db_client: object):
        super().__init__(db_client=db_client)

    @classmethod
    async def create_instance(cls, db_client: object):
        return cls(db_client)

    async def enqueue(
        self,
        client_id: str,
        session_id,
        patient_message: str,
        draft_reply: str,
        rejection_reason: str,
        rejection_detail: dict | None = None,
    ) -> HumanHandoffQueue:
        entry = HumanHandoffQueue(
            client_id=client_id,
            session_id=session_id,
            patient_message=patient_message,
            draft_reply=draft_reply,
            rejection_reason=rejection_reason,
            rejection_detail=rejection_detail,
        )
        async with self.db_client() as session:
            async with session.begin():
                session.add(entry)
            await session.commit()
            await session.refresh(entry)
        return entry
