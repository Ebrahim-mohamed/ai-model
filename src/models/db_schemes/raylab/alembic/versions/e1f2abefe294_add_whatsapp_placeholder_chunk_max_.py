"""add whatsapp_placeholder_chunk_max_distinct_ratio to client_config

Revision ID: e1f2abefe294
Revises: a2c8f5e91d34
Create Date: 2026-09-08 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e1f2abefe294'
down_revision: Union[str, None] = 'a2c8f5e91d34'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Dynamic Cross-Brand Availability Pipeline (2026-09-08, re-
    # implemented with a deterministic Hallucination Lock) — see
    # TextReplyController._is_placeholder_shaped_chunk's own docstring
    # and client_config.py's own column comment for the full real
    # incident and calibration evidence (0.3 ratio on a real confirmed
    # "not available" chunk vs. 1.0 on a real informative one).
    op.add_column('client_config', sa.Column('whatsapp_placeholder_chunk_max_distinct_ratio', sa.Float(), server_default='0.4', nullable=False))


def downgrade() -> None:
    op.drop_column('client_config', 'whatsapp_placeholder_chunk_max_distinct_ratio')
