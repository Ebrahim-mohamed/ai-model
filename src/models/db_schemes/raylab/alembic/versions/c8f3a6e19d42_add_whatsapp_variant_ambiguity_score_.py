"""add whatsapp_variant_ambiguity_score_gap to client_config

Revision ID: c8f3a6e19d42
Revises: b7d2e4f61a93
Create Date: 2026-09-08 00:00:00.000000

Dynamic Disambiguation / Clarification Flow (2026-09-08) — powers
TextReplyController._detect_variant_ambiguity: the maximum reranker-score
gap, among distinct-named candidates sharing a real common leading-token
prefix in the already-fetched broad candidate set, for them to be treated
as genuinely competing sub-variants worth asking the patient to
disambiguate (e.g. 'CBCT (3D) Single Arch' vs 'CBCT (3D) Both Arches').
Same pattern as whatsapp_breadth_score_gap (client_config.py) — a business
threshold, never a Python literal (claude.md §1.3). No real-traffic
calibration data exists yet for this specific value (unlike
whatsapp_breadth_score_gap's own 5-narrow/3-broad real sample) — 0.3
starting point chosen to match breadth_score_gap's own already-calibrated
value on the same reranker score scale, pending real disambiguation
traffic to tune it. A smaller gap trades toward fewer, more confident
clarification prompts (favoring the more costly failure mode this feature
was built to avoid: guessing wrong, per the real CBCT Single Arch vs Both
Arches misclassification that motivated it) over annoying an already-clear
query with an unnecessary extra turn.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c8f3a6e19d42'
down_revision: Union[str, None] = 'b7d2e4f61a93'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'client_config',
        sa.Column('whatsapp_variant_ambiguity_score_gap', sa.Float(), server_default='0.3', nullable=False),
    )


def downgrade() -> None:
    op.drop_column('client_config', 'whatsapp_variant_ambiguity_score_gap')
