from .BaseDataModel import BaseDataModel
from .db_schemes.raylab.schemes import IntentLog


class RoutingOutcome:
    """Closed set for IntentLog.routing_outcome — plain string values,
    same convention as MessageDirection. AI_HANDLED, ESCALATED_HUMAN, and
    PENDING_CLARIFICATION all count as the system working correctly
    (claude.md §6 / Phase 6's dashboard design) — none of these are
    'failures' to filter out of analytics."""

    AI_HANDLED = "ai_handled"
    ESCALATED_HUMAN = "escalated_human"
    PENDING_CLARIFICATION = "pending_clarification"
    NOT_IMPLEMENTED = "not_implemented"  # Steps 2-5 phases not yet built


class IntentLogModel(BaseDataModel):
    """Repository for intent_log — Phase 6's analytics log. Every method
    requires client_id, no exceptions. Append-only, write-once-per-turn:
    called from exactly one call site, IntentRoutingController, never
    re-implemented per phase (claude.md §6.3)."""

    def __init__(self, db_client: object):
        super().__init__(db_client=db_client)

    @classmethod
    async def create_instance(cls, db_client: object):
        return cls(db_client)

    async def log_turn(
        self,
        client_id: str,
        session_id,
        modality: str,
        intent: str,
        routing_outcome: str,
        resolution_status: str | None = None,
    ) -> IntentLog:
        entry = IntentLog(
            client_id=client_id,
            session_id=session_id,
            modality=modality,
            intent=intent,
            routing_outcome=routing_outcome,
            resolution_status=resolution_status,
        )
        async with self.db_client() as session:
            async with session.begin():
                session.add(entry)
            await session.commit()
            await session.refresh(entry)
        return entry
