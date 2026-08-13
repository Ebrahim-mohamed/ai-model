"""add whatsapp_min_relevance_score to client_config

Revision ID: c3f7a1b2d4e6
Revises: deb4535fe6cf
Create Date: 2026-08-14 10:05:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3f7a1b2d4e6'
down_revision: Union[str, None] = 'deb4535fe6cf'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('client_config', sa.Column('whatsapp_min_relevance_score', sa.Float(), server_default='0.0', nullable=False))


def downgrade() -> None:
    op.drop_column('client_config', 'whatsapp_min_relevance_score')
