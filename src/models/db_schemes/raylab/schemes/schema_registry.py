from sqlalchemy import Column, String, DateTime, func
from sqlalchemy.dialects.postgresql import ARRAY

from .raylab_base import SQLAlchemyBase


class SchemaRegistry(SQLAlchemyBase):
    """Auto-discovered, auto-registered per-sheet shape. One row per
    (client_id, sheet_name), written by the pipeline itself the first time
    a sheet is encountered during a sync — never hand-typed by an admin.

    `bucket` stores a BucketEnum value as plain text (not a Postgres native
    enum): this file is imported by Alembic with only the `models/db_schemes/
    raylab/` directory on sys.path, where `models.enums.BucketEnum` can't be
    resolved. The closed set is enforced where it's actually reachable —
    SchemaRegistryModel, which runs from `src/` — exactly like `chunk_type`
    on KnowledgeChunk.
    """

    __tablename__ = "schema_registry"

    client_id = Column(String, primary_key=True)
    sheet_name = Column(String, primary_key=True)

    columns = Column(ARRAY(String), nullable=False)
    bucket = Column(String, nullable=False, server_default="VECTOR_DB")
    mandatory_fields = Column(ARRAY(String), nullable=False, server_default="{}")

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now(), nullable=True)
