from sqlalchemy import Column, String, Float, Integer, Text, DateTime, Index, func, text
from sqlalchemy.dialects.postgresql import UUID

from .raylab_base import SQLAlchemyBase


class ShootoutResult(SQLAlchemyBase):
    """One row per (client_id, model_name) shootout run. top_k_accuracy
    and error_message are both nullable — a candidate that can't run at
    all (e.g. Swan-Large's gated-access/hardware blocker, see README's
    Step 8 section) still gets a row recording *why*, never a silently
    missing result (claude.md's no-silent-failure discipline)."""

    __tablename__ = "shootout_results"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))

    client_id = Column(String, nullable=False)
    model_name = Column(String, nullable=False)

    top_k_accuracy = Column(Float, nullable=True)
    query_count = Column(Integer, nullable=False)
    top_k = Column(Integer, nullable=False)
    error_message = Column(Text, nullable=True)

    evaluated_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("idx_shootout_results_client", client_id),
    )
