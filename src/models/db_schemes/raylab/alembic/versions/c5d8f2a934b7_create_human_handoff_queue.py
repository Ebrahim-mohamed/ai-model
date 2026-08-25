"""create human_handoff_queue

Revision ID: c5d8f2a934b7
Revises: a7c2e9f4b8d1
Create Date: 2026-08-25 19:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'c5d8f2a934b7'
down_revision: Union[str, None] = 'a7c2e9f4b8d1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('human_handoff_queue',
    sa.Column('id', postgresql.UUID(as_uuid=True), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('client_id', sa.String(), nullable=False),
    sa.Column('session_id', postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column('patient_message', sa.Text(), nullable=False),
    sa.Column('draft_reply', sa.Text(), nullable=False),
    sa.Column('rejection_reason', sa.String(), nullable=False),
    sa.Column('rejection_detail', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('status', sa.String(), server_default='open', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(
        'idx_human_handoff_queue_client_status', 'human_handoff_queue',
        ['client_id', 'status', 'created_at'], unique=False,
    )


def downgrade() -> None:
    op.drop_index('idx_human_handoff_queue_client_status', table_name='human_handoff_queue')
    op.drop_table('human_handoff_queue')
