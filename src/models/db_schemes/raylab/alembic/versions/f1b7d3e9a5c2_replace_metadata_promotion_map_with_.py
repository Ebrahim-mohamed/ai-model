"""replace metadata_promotion_map with brand_value_aliases

Revision ID: f1b7d3e9a5c2
Revises: e6a29c04f8b1
Create Date: 2026-08-24 10:05:00.000000

Supersedes e6a29c04f8b1's label-keyed metadata_promotion_map
({"Acoount": "brand"}), found live against real data to be insufficient:
brand/account information actually appears under 4 different column
spellings (account / Account / Accounts / Acoount) across 5 sheets, and
critically, in 2 different scripts (Examinations stores English
"Technoscan"/"Cairoscan"; Branch Directory, Sheet1, Weights, and
Anesthesia all store Arabic "تكنوسكان"/"كايروسكان") — a label-keyed map
would need updating every time a new sheet used yet another spelling of
the same column, which real data already shows happens often.

brand_value_aliases instead matches by VALUE, not column name — a small,
closed, business-owned vocabulary of the real brand strings this client
actually uses, mapped to one canonical lowercase value. ChunkingController
scans every field's value (regardless of which column or sheet it came
from) against this map, so a brand-new sheet with the same column spelled
yet another way (or in either language) is picked up automatically, with
zero code or config change — the column/sheet/spelling axis becomes fully
dynamic; only the actual business vocabulary (a new brand, or a new way of
writing an existing one) is a deliberate, one-time config edit, matching
claude.md §3.1's standing rule against inferring business semantics
automatically.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision: str = 'f1b7d3e9a5c2'
down_revision: Union[str, None] = 'e6a29c04f8b1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column('client_config', 'metadata_promotion_map')
    op.add_column('client_config', sa.Column('brand_value_aliases', JSONB, server_default='{}', nullable=False))


def downgrade() -> None:
    op.drop_column('client_config', 'brand_value_aliases')
    op.add_column('client_config', sa.Column('metadata_promotion_map', JSONB, server_default='{}', nullable=False))
