from sqlalchemy import Column, String, DateTime, func
from sqlalchemy.dialects.postgresql import ARRAY

from .raylab_base import SQLAlchemyBase


class SchemaRegistry(SQLAlchemyBase):
    """Auto-discovered, auto-registered per-sheet shape. One row per
    (client_id, sheet_name), written by the pipeline itself the first time
    a sheet is encountered during a sync — never hand-typed by an admin.

    No `bucket` column (architecture override, see claude.md): OneDrive
    sync is strictly Bucket A now — the data entry team never uploads
    Bucket B/C content, so there is nothing left to route dynamically.
    Bucket B/C are static, hardcoded templates decoupled entirely from
    this table — see stores/llm/templates/static/.
    """

    __tablename__ = "schema_registry"

    client_id = Column(String, primary_key=True)
    sheet_name = Column(String, primary_key=True)

    columns = Column(ARRAY(String), nullable=False)
    mandatory_fields = Column(ARRAY(String), nullable=False, server_default="{}")

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now(), nullable=True)
