from sqlalchemy import Column, String, DateTime, Float, Index, func, text
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

    # Analytics Dashboard pipeline (2026-09-08) — see the
    # b7d2e4f61a93 migration's own module docstring for the full
    # reasoning behind each of these four columns, including why
    # outcome_status is a separate concept from routing_outcome above,
    # and why retrieval_score is audit-trail-only, never a not_found
    # classifier input. All four are NULL for a turn that never reached
    # TextReplyController._mode_a_reply (Mode B substitution, or a
    # not-yet-implemented routing target) except latency_ms, which is
    # universal — recorded for every turn regardless of routing target.
    outcome_status = Column(String, nullable=True)     # "answered" | "not_found" | "escalated_verification" | "technical_error" | None
    extracted_topic = Column(String, nullable=True)     # closed-set canonical topic, from classify_topic
    retrieval_score = Column(Float, nullable=True)       # top-1 retrieval/rerank score, diagnostic only
    latency_ms = Column(Float, nullable=True)            # end-to-end route_turn duration

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        # The dashboard's own access patterns: volume/trend over time,
        # category breakdown, rate calculations, and topic breakdown.
        Index("idx_intent_log_client_created", client_id, created_at),
        Index("idx_intent_log_client_intent", client_id, intent),
        Index("idx_intent_log_client_outcome_created", client_id, outcome_status, created_at),
        Index("idx_intent_log_client_topic", client_id, extracted_topic),
    )
