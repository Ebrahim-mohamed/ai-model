"""add analytics dashboard tracking columns to intent_log and topic_taxonomy to client_config

Revision ID: b7d2e4f61a93
Revises: e1f2abefe294
Create Date: 2026-09-08 00:00:00.000000

Analytics Dashboard pipeline (2026-09-08) — persists structural facts
TextReplyController._mode_a_reply already computes live, mid-turn, and
previously discarded once the reply text was returned:

- outcome_status: "answered" | "not_found" | "escalated_verification" |
  "technical_error" — deliberately a NEW, separate column from the
  existing routing_outcome, not an overload of it. routing_outcome is a
  routing-level concept (IntentLogModel.RoutingOutcome's own docstring:
  "AI_HANDLED, ESCALATED_HUMAN, and PENDING_CLARIFICATION all count as
  the system working correctly... none of these are 'failures'").
  outcome_status is narrower and specific to Mode A: did a grounded-
  search attempt find real catalog coverage for the patient's question.
  NULL for any turn that never reached _mode_a_reply at all (Mode B
  verbatim substitution, or a not-yet-implemented routing target) —
  those aren't RAG search attempts and have no meaningful answered/
  not_found status.

- extracted_topic: the canonical exam/service name a turn was about,
  from QueryRouterInterface.classify_topic's closed-set classification
  against client_config.topic_taxonomy — never a raw/free-text query
  string (see that method's own docstring for why: field_data's real
  column keys are per-sheet dynamic, confirmed via ChunkingController,
  so there is no single reliable metadata key to read a canonical name
  from directly).

- retrieval_score: the top-1 retrieval/rerank score for this turn's
  primary search — audit trail only. Deliberately NOT used to derive
  outcome_status: _mode_a_reply's own comment block (the "No relevance-
  score gate here (tried and removed)" note) already documents real
  evidence that a low top-1 score does NOT reliably mean "wrong/no
  answer" — genuinely correct narrow-query answers were observed
  scoring under the same kind of threshold this column could tempt a
  future not_found rule to use. Kept for future threshold-tuning
  analysis, not as an active classifier input.

- latency_ms: end-to-end wall-clock duration of the whole
  IntentRoutingController.route_turn call, persisted for every turn
  regardless of routing target (unlike the three columns above, this
  concept is universal, not RAG-search-specific).

client_config.topic_taxonomy: closed-set vocabulary for classify_topic,
same value-curated-by-admin bootstrap story as brand_value_aliases —
starts empty ('[]'), every classification degrades to "unclassified"
until real canonical exam/service names are added here.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision: str = 'b7d2e4f61a93'
down_revision: Union[str, None] = 'e1f2abefe294'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('intent_log', sa.Column('outcome_status', sa.String(), nullable=True))
    op.add_column('intent_log', sa.Column('extracted_topic', sa.String(), nullable=True))
    op.add_column('intent_log', sa.Column('retrieval_score', sa.Float(), nullable=True))
    op.add_column('intent_log', sa.Column('latency_ms', sa.Float(), nullable=True))

    # The dashboard's own two new access patterns: rate calculations
    # (client_id + outcome_status, filtered by created_at range) and
    # topic-breakdown GROUP BY (client_id + extracted_topic).
    op.create_index(
        'idx_intent_log_client_outcome_created', 'intent_log',
        ['client_id', 'outcome_status', 'created_at'],
    )
    op.create_index(
        'idx_intent_log_client_topic', 'intent_log',
        ['client_id', 'extracted_topic'],
    )

    op.add_column(
        'client_config',
        sa.Column('topic_taxonomy', JSONB, server_default='[]', nullable=False),
    )


def downgrade() -> None:
    op.drop_column('client_config', 'topic_taxonomy')
    op.drop_index('idx_intent_log_client_topic', table_name='intent_log')
    op.drop_index('idx_intent_log_client_outcome_created', table_name='intent_log')
    op.drop_column('intent_log', 'latency_ms')
    op.drop_column('intent_log', 'retrieval_score')
    op.drop_column('intent_log', 'extracted_topic')
    op.drop_column('intent_log', 'outcome_status')
