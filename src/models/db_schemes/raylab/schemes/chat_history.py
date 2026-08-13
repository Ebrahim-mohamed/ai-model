from sqlalchemy import Column, String, Text, DateTime, Index, func, text
from sqlalchemy.dialects.postgresql import UUID

from .raylab_base import SQLAlchemyBase


class ChatHistory(SQLAlchemyBase):
    """Tier 2 (PostgreSQL) of Section 3 Step 1's two-tier chat-history
    architecture — the permanent record. Append-only: one row per
    message, both directions, never overwritten. Tier 1 (Redis) is the
    short-lived working copy this table backs; see utils/session_store.py
    for the rolling-expiry/hydration mechanics."""

    __tablename__ = "chat_history"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))

    # The session anchor — UUID specifically so a session created during a
    # burst of concurrent conversations can never collide with another
    # one (claude.md §6.2's stated reason for this exact choice).
    session_id = Column(UUID(as_uuid=True), nullable=False)

    client_id = Column(String, nullable=False)  # Tenant isolation key — mandatory, universal
    direction = Column(String, nullable=False)  # "inbound" | "outbound" — see MessageDirection
    content = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        # The hydration query's own access pattern: "last N messages for
        # this client's session", ordered by recency.
        Index("idx_chat_history_client_session", client_id, session_id, created_at),
    )
