"""drop whatsapp_field_selection_gap_threshold, reset max_fields to 1

Revision ID: d4f61c8b3a92
Revises: f4a7d2e91c3b
Create Date: 2026-08-23 20:15:00.000000

Cleans up real schema drift found live on the raylab-pgvector database:
a dynamic gap-based field-selection experiment added
whatsapp_field_selection_gap_threshold (server_default 0.05) directly
against the DB and reset whatsapp_field_selection_max_fields back to 2,
outside of any tracked migration (alembic_version was found stamped at
'b2c8f5e91a4d', a revision with no corresponding file anywhere in this
versions/ directory). The code side of that experiment was already
reverted (FieldSelectionController.py has no gap logic; client_config.py
went back to max_fields server_default='1'), but the live schema was
never rolled back to match — this migration is that rollback, made real
and tracked instead of another untracked ALTER TABLE. The DB was
re-stamped to f4a7d2e91c3b (the last real file-tracked revision) before
this one was authored, so down_revision below is accurate.

Real-data grounding for the max_fields=1 decision itself (not repeated
here in full): scripts/finetune_data/sampling.py's narrow_positive pass
builds every training/val example as a single "label: value" line by
construction — 1 exactly matches that shape; 2 was verified handing the
model an untrained-on shape in a real failing case (elevator-availability
query, which surfaced 2 fields where training only ever showed 1).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4f61c8b3a92'
down_revision: Union[str, None] = 'f4a7d2e91c3b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column('client_config', 'whatsapp_field_selection_gap_threshold')
    op.execute("UPDATE client_config SET whatsapp_field_selection_max_fields = 1")
    op.alter_column('client_config', 'whatsapp_field_selection_max_fields', server_default='1')


def downgrade() -> None:
    op.alter_column('client_config', 'whatsapp_field_selection_max_fields', server_default='2')
    op.execute("UPDATE client_config SET whatsapp_field_selection_max_fields = 2")
    op.add_column('client_config', sa.Column('whatsapp_field_selection_gap_threshold', sa.Float(), server_default='0.05', nullable=False))
