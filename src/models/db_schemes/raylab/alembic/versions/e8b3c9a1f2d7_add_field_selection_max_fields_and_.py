"""add whatsapp_field_selection_max_fields, repurpose whatsapp_min_relevance_score default

Revision ID: e8b3c9a1f2d7
Revises: c3f7a1b2d4e6
Create Date: 2026-08-15 09:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e8b3c9a1f2d7'
down_revision: Union[str, None] = 'c3f7a1b2d4e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('client_config', sa.Column('whatsapp_field_selection_max_fields', sa.Integer(), server_default='2', nullable=False))
    # whatsapp_min_relevance_score is repurposed (Mode A relevance gate,
    # now removed -> FieldSelectionController's cosine-similarity floor).
    # Its old default (0.0) was calibrated for the reranker's unbounded
    # logit scale and is not a sensible cosine-similarity floor. Existing
    # rows are updated explicitly rather than left at a value calibrated
    # for a mechanism that no longer reads this column.
    op.execute("UPDATE client_config SET whatsapp_min_relevance_score = 0.35")
    op.alter_column('client_config', 'whatsapp_min_relevance_score', server_default='0.35')


def downgrade() -> None:
    op.alter_column('client_config', 'whatsapp_min_relevance_score', server_default='0.0')
    op.execute("UPDATE client_config SET whatsapp_min_relevance_score = 0.0")
    op.drop_column('client_config', 'whatsapp_field_selection_max_fields')
