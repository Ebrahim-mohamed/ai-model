from sqlalchemy import Column, String, DateTime, Index, func, text
from sqlalchemy.dialects.postgresql import UUID

from .raylab_base import SQLAlchemyBase


class IntentLog(SQLAlchemyBase):
    """Phase 6's Intent Log — written once, at the routing gate, for
    every classified turn, regardless of outcome (claude.md §6 /
    Implementation Plan §Foundation). Append-only, never conditioned on
    success: an escalation, a decline-to-guess, or a low-confidence
    fallback is still a logged row, not a gap in the analytics."""

    __tablename__ = "intent_log"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))

    client_id = Column(String, nullable=False)  # Tenant isolation key — mandatory, universal
    session_id = Column(UUID(as_uuid=True), nullable=False)

    modality = Column(String, nullable=False)          # "text" | "voice" | "image"
    intent = Column(String, nullable=False)             # from utils/intent_routing_map.py's closed set
    routing_outcome = Column(String, nullable=False)    # "ai_handled" | "escalated_human" | "pending_clarification" | ...
    resolution_status = Column(String, nullable=True)   # set later, once an outcome is known; never blocks the initial write

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        # The dashboard's own two access patterns: volume/trend over time,
        # and category breakdown.
        Index("idx_intent_log_client_created", client_id, created_at),
        Index("idx_intent_log_client_intent", client_id, intent),
    )
