"""add whatsapp_breadth_score_threshold and whatsapp_breadth_score_gap to client_config

Revision ID: f4a7d2e91c3b
Revises: e8b3c9a1f2d7
Create Date: 2026-08-15 14:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f4a7d2e91c3b'
down_revision: Union[str, None] = 'e8b3c9a1f2d7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('client_config', sa.Column('whatsapp_breadth_score_threshold', sa.Float(), server_default='0.0', nullable=False))
    op.add_column('client_config', sa.Column('whatsapp_breadth_score_gap', sa.Float(), server_default='0.3', nullable=False))


def downgrade() -> None:
    op.drop_column('client_config', 'whatsapp_breadth_score_gap')
    op.drop_column('client_config', 'whatsapp_breadth_score_threshold')
