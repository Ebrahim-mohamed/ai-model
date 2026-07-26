from sqlalchemy import Column, String, DateTime, Index, func, text
from sqlalchemy.dialects.postgresql import UUID, JSONB

from .raylab_base import SQLAlchemyBase


class StagingRow(SQLAlchemyBase):
    """Parsed-but-unchunked rows — Bucket A only (claude.md §3.5). Bucket B
    and Bucket C never land here; they're rendered straight to generated
    template files by utils/template_file_writer.py instead."""

    __tablename__ = "staging_rows"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))

    client_id = Column(String, nullable=False)
    sheet_name = Column(String, nullable=False)
    bucket = Column(String, nullable=False)  # always BucketEnum.VECTOR_DB.value here

    # The row's own fields, keyed by column name, PLUS a "sheet_name" key
    # injected by the same generic parse loop that populates everything
    # else — claude.md §3.6, never a per-sheet annotation.
    row_data = Column(JSONB, nullable=False)

    source_file = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("idx_staging_rows_client_sheet", client_id, sheet_name),
        Index("idx_staging_rows_source_file", client_id, source_file),
    )
