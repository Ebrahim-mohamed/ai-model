"""raise whatsapp_retrieval_top_k_narrow default 1 -> 3

Revision ID: d9f4a2e7c1b6
Revises: c5d8f2a934b7
Create Date: 2026-08-26 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd9f4a2e7c1b6'
down_revision: Union[str, None] = 'c5d8f2a934b7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # server_default only governs future INSERTs -- the explicit UPDATE
    # below is what actually moves the real, currently-onboarded raylab
    # row (and any other existing client row) onto the new value.
    op.alter_column('client_config', 'whatsapp_retrieval_top_k_narrow', server_default='3')
    op.execute("UPDATE client_config SET whatsapp_retrieval_top_k_narrow = 3")


def downgrade() -> None:
    op.execute("UPDATE client_config SET whatsapp_retrieval_top_k_narrow = 1")
    op.alter_column('client_config', 'whatsapp_retrieval_top_k_narrow', server_default='1')
