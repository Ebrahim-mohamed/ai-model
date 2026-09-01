"""add whatsapp_sheet_filter_score_threshold to client_config

Revision ID: a2c8f5e91d34
Revises: b3a9f5c2d8e1
Create Date: 2026-09-02 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a2c8f5e91d34'
down_revision: Union[str, None] = 'b3a9f5c2d8e1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Dynamic metadata pre-filtering (2026-09-02): the two-tier retrieval
    # fallback needs its own, separately-calibrated acceptance threshold —
    # deliberately NOT reusing whatsapp_breadth_score_threshold/
    # whatsapp_breadth_score_gap, which real evidence earlier in this
    # project already showed miscalibrated (this reranker's real scores
    # are routinely negative even for correct matches; an absolute floor
    # of 0.0 rejected a confirmed-correct match once). Starting value is
    # an uncalibrated guess, same "starting point, not a proven number"
    # status every other threshold in this project carries until real
    # traffic tunes it — never trust this default blindly.
    op.add_column('client_config', sa.Column('whatsapp_sheet_filter_score_threshold', sa.Float(), server_default='-5.0', nullable=False))


def downgrade() -> None:
    op.drop_column('client_config', 'whatsapp_sheet_filter_score_threshold')
