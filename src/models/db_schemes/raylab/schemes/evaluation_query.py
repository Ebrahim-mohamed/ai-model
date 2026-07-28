from sqlalchemy import Column, String, Text, DateTime, ForeignKey, Index, func, text
from sqlalchemy.dialects.postgresql import UUID

from .raylab_base import SQLAlchemyBase


class EvaluationQuery(SQLAlchemyBase):
    """The shared benchmark query set Step 8's shootout scores every
    candidate model against — one real, natural-language question paired
    with the real knowledge_chunks row that correctly answers it. Every
    query here is grounded in this client's actual synced content, never
    fabricated business facts (claude.md §4.3) — only the questions
    themselves are a designed evaluation harness, exactly as the proposal
    describes for bootstrapping a benchmark before real user query logs
    exist."""

    __tablename__ = "evaluation_queries"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))

    client_id = Column(String, nullable=False)
    query_text = Column(Text, nullable=False)
    expected_chunk_id = Column(UUID(as_uuid=True), ForeignKey("knowledge_chunks.id"), nullable=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("idx_evaluation_queries_client", client_id),
    )
