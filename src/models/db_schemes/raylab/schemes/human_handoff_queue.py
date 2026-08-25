from sqlalchemy import Column, String, Text, DateTime, Index, func, text
from sqlalchemy.dialects.postgresql import UUID, JSONB

from .raylab_base import SQLAlchemyBase


class HumanHandoffQueue(SQLAlchemyBase):
    """Section 3 Step 1's reply-verification safety net (Implementation
    Plan's post-fine-tuning Step 8). Written exactly once per rejected
    turn, by `ReplyVerificationController` alone, when a Mode A reply's
    phrasing makes a numeric claim its own extracted `debug_json` doesn't
    support — the same deterministic auto-reject signal
    `scripts/finetune_data/grounding_gate.py::_check_phrasing_numeric_grounding`
    already proved has zero false positives standalone, now applied to
    live output instead of training data. `draft_reply` is the real,
    withheld model output — never shown to the patient, kept here so a
    human agent has the actual candidate answer to review, not just the
    fact that something went wrong."""

    __tablename__ = "human_handoff_queue"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))

    client_id = Column(String, nullable=False)  # Tenant isolation key — mandatory, universal
    session_id = Column(UUID(as_uuid=True), nullable=False)

    patient_message = Column(Text, nullable=False)
    draft_reply = Column(Text, nullable=False)          # the withheld, unverified reply text
    rejection_reason = Column(String, nullable=False)   # e.g. "unverified_numeric_claim"
    rejection_detail = Column(JSONB, nullable=True)      # the offending sentence/number, for review

    # Plain string, same convention as MessageDirection/RoutingOutcome — no
    # workflow beyond "still needs a human" exists yet (claude.md §6.3: a
    # new complaint/booking-style workflow around this status is a later,
    # explicitly-scoped step, not something to pre-build here).
    status = Column(String, nullable=False, server_default="open")

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        # A human agent's own access pattern: open items for this client,
        # oldest first — same shape as intent_log's client+created index.
        Index("idx_human_handoff_queue_client_status", client_id, status, created_at),
    )
