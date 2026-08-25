"""drop field-selection columns after FieldSelectionController removal

Revision ID: a7c2e9f4b8d1
Revises: f1b7d3e9a5c2
Create Date: 2026-08-25 00:00:00.000000

FieldSelectionController is deleted (its single caller,
TextReplyController._narrow_context_block, now hands the model each
retrieved chunk's full, real content unfiltered, matching the retrained
model's own input shape — scripts/finetune_data/sampling.py's Pass 1,
2026-08-25 redesign). whatsapp_min_relevance_score and
whatsapp_field_selection_max_fields existed solely to parameterize that
controller's similarity floor and field cap; with no remaining caller,
both are dead config, not just unused — dropped rather than left behind.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7c2e9f4b8d1'
down_revision: Union[str, None] = 'f1b7d3e9a5c2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column('client_config', 'whatsapp_min_relevance_score')
    op.drop_column('client_config', 'whatsapp_field_selection_max_fields')


def downgrade() -> None:
    op.add_column('client_config', sa.Column('whatsapp_min_relevance_score', sa.Float(), server_default='0.35', nullable=False))
    op.add_column('client_config', sa.Column('whatsapp_field_selection_max_fields', sa.Integer(), server_default='1', nullable=False))
