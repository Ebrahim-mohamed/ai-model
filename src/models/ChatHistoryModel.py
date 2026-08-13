from sqlalchemy.future import select

from .BaseDataModel import BaseDataModel
from .db_schemes.raylab.schemes import ChatHistory


class MessageDirection:
    """Closed set for ChatHistory.direction — plain string values (same
    DB representation convention as every other closed-set field in this
    project, e.g. embedding_backend_override), never compared against a
    bare literal outside this class."""

    INBOUND = "inbound"
    OUTBOUND = "outbound"


class ChatHistoryModel(BaseDataModel):
    """Repository for chat_history — Tier 2 of the two-tier architecture.
    Every method requires client_id, no exceptions. Append-only: there is
    deliberately no update/delete method here, matching the design (a
    permanent record, not a mutable current-state row)."""

    def __init__(self, db_client: object):
        super().__init__(db_client=db_client)

    @classmethod
    async def create_instance(cls, db_client: object):
        return cls(db_client)

    async def append_message(self, client_id: str, session_id, direction: str, content: str) -> ChatHistory:
        message = ChatHistory(
            client_id=client_id,
            session_id=session_id,
            direction=direction,
            content=content,
        )
        async with self.db_client() as session:
            async with session.begin():
                session.add(message)
            await session.commit()
            await session.refresh(message)
        return message

    async def get_recent_messages(self, client_id: str, session_id, limit: int = 10) -> list[ChatHistory]:
        """The hydration query (Implementation Plan Step 1): the last
        `limit` messages for this client's session, returned oldest-first
        so they can be fed directly into an LLM's conversation history
        without the caller having to reverse them."""
        async with self.db_client() as session:
            stmt = (
                select(ChatHistory)
                .where(ChatHistory.client_id == client_id, ChatHistory.session_id == session_id)
                .order_by(ChatHistory.created_at.desc())
                .limit(limit)
            )
            result = await session.execute(stmt)
            rows = list(result.scalars().all())
        return list(reversed(rows))
