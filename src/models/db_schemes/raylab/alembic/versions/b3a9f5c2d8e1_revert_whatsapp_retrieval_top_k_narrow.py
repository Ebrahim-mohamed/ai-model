"""revert whatsapp_retrieval_top_k_narrow default 3 -> 1

Revision ID: b3a9f5c2d8e1
Revises: d9f4a2e7c1b6
Create Date: 2026-08-26 12:00:00.000000

Reverts d9f4a2e7c1b6 (a new forward migration, not an edit-in-place of
that one — never hand-edit an already-applied migration). A real-data
retrieval investigation (scripts/investigate_retrieval_task1.py) found
the top_k_narrow=3 + wrapped-narrow-context change caused genuine
regressions: cases with an overwhelming, unambiguous single correct
chunk still failed to extract once presented as multiple wrapped
sources, and at least one case showed a concrete distractor row (same
field label, different company, different number) that only entered the
window because top_k_narrow exceeded 1. See client_config.py's own
column comment and TextReplyController._narrow_context_block's docstring
for the full history. TextReplyController's widening retry (Solution 2)
now carries the "give the model more chunks" responsibility instead,
triggered only when the single unwrapped chunk's own extraction comes
back completely empty.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b3a9f5c2d8e1'
down_revision: Union[str, None] = 'd9f4a2e7c1b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column('client_config', 'whatsapp_retrieval_top_k_narrow', server_default='1')
    op.execute("UPDATE client_config SET whatsapp_retrieval_top_k_narrow = 1")


def downgrade() -> None:
    op.execute("UPDATE client_config SET whatsapp_retrieval_top_k_narrow = 3")
    op.alter_column('client_config', 'whatsapp_retrieval_top_k_narrow', server_default='3')
