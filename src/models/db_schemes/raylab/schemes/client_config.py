from sqlalchemy import Column, String, Float
from sqlalchemy.dialects.postgresql import ARRAY

from .raylab_base import SQLAlchemyBase


class ClientConfig(SQLAlchemyBase):
    """The tenant registry. One row per client_id — every value that would
    otherwise be a hardcoded Python constant lives here instead."""

    __tablename__ = "client_config"

    client_id = Column(String, primary_key=True)
    allowed_metadata_keys = Column(ARRAY(String), nullable=False, server_default="{}")
    boilerplate_threshold = Column(Float, nullable=False, server_default="0.9")
    onedrive_item_id = Column(String, nullable=True)
    embedding_backend_override = Column(String, nullable=True)

    # Step 4 — the minimal "authenticated admin session" stand-in: POST
    # /api/sync resolves client_id from this key server-side, never from a
    # client-supplied field. Section 2 has no full admin-auth system in
    # scope; this is the smallest mechanism that still makes the resolution
    # server-controlled rather than trusting the caller's word for it.
    admin_api_key = Column(String, nullable=True, unique=True)
