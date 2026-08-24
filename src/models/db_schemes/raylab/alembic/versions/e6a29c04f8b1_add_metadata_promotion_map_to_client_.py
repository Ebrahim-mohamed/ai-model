"""add metadata_promotion_map to client_config

Revision ID: e6a29c04f8b1
Revises: d4f61c8b3a92
Create Date: 2026-08-24 09:10:00.000000

Config-driven field_data -> top-level metadata promotion (claude.md §1.3 —
never a hardcoded column/sheet name in a controller). Maps a field_data
label (e.g. the real, misspelled source column "Acoount") to the clean,
normalized top-level metadata key ChunkingController should also write it
under (e.g. "brand"), so RetrievalController's metadata_filters can query
it directly instead of needing to reach into the nested field_data dict.
Per-client, since which columns carry a filterable dimension (and what to
call it) is real business data, not a structural fact about the schema.

Real values for the raylab client (brand filtering, Examinations sheet
only — confirmed live against the DB: no other synced sheet has an
"Acoount" column) are set via a separate UPDATE, not baked into this
migration's upgrade() — this migration only adds the empty-default
column; client-specific config values were never embedded in prior
migrations either (see e.g. admin_api_key, onedrive_item_id).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision: str = 'e6a29c04f8b1'
down_revision: Union[str, None] = 'd4f61c8b3a92'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('client_config', sa.Column('metadata_promotion_map', JSONB, server_default='{}', nullable=False))


def downgrade() -> None:
    op.drop_column('client_config', 'metadata_promotion_map')
