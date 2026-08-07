import uuid

from sqlalchemy import Column, String, Text, Date, DateTime, Index, func, text
from sqlalchemy.dialects.postgresql import UUID, JSONB
from pgvector.sqlalchemy import Vector

from .raylab_base import SQLAlchemyBase


class KnowledgeChunk(SQLAlchemyBase):
    """The pgvector store. One row per embeddable chunk, for any client, any bucket-A sheet."""

    __tablename__ = "knowledge_chunks"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))

    client_id = Column(String, nullable=False)  # Tenant isolation key — mandatory, universal
    content = Column(Text, nullable=False)
    embedding = Column(Vector(1024))
    source_file = Column(String, nullable=True)
    chunk_type = Column(String, nullable=True)  # client-defined label, e.g. 'branch', 'lab_test'

    # "metadata" is reserved on the declarative base (Base.metadata); the
    # Python attribute is renamed, the DB column stays exactly "metadata".
    metadata_payload = Column("metadata", JSONB, nullable=False, server_default="{}")

    valid_from = Column(Date, nullable=True)
    valid_until = Column(Date, nullable=True)
    last_synced_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("idx_chunks_client", client_id),
        Index("idx_chunks_metadata", metadata_payload, postgresql_using="gin"),
        Index(
            "idx_chunks_embedding_hnsw",
            embedding,
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        # The sparse/BM25 leg of Step 9's hybrid search (PGVectorProvider.
        # search_by_bm25) was measured recomputing to_tsvector('simple', ...)
        # for every row on every request — a real ~1.4s cost, confirmed via
        # EXPLAIN ANALYZE, that gets worse as the corpus grows. This
        # expression index must use the exact same function call
        # (to_tsvector('simple', content)) as that query for the planner to
        # actually use it.
        Index(
            "idx_chunks_content_fts",
            text("to_tsvector('simple', content)"),
            postgresql_using="gin",
        ),
    )
