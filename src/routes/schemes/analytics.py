from datetime import datetime

from pydantic import BaseModel


class Period(BaseModel):
    # period_from/period_to, not from/to — "from" is a reserved Python
    # keyword and can't be a plain BaseModel field name; every other
    # schema file in this codebase (routes/schemes/*.py) is a flat
    # BaseModel with no alias/Config magic, so this follows that same
    # plain convention rather than reaching for Pydantic v2's Field(alias=)
    # just to preserve a "from"/"to" wire shape.
    period_from: datetime
    period_to: datetime


class KPIs(BaseModel):
    total_searches: int
    # 2026-09-08 addendum: every *_rate field is a 0-100 percentage
    # (e.g. 72.37), not a 0-1 fraction — scaled and rounded in
    # AnalyticsController.get_dashboard's own _rate helper, for direct
    # dashboard UI presentation.
    answered_rate: float
    not_found_rate: float
    escalated_verification_rate: float
    technical_error_rate: float
    clarification_requested_rate: float
    escalated_human_rate: float
    pending_rate: float
    # Additive, not a replacement for escalated_human_rate above — see
    # AnalyticsController.get_dashboard's own comment on
    # human_handoff_offered_count for why these are deliberately
    # different numbers.
    human_handoff_offered_rate: float
    avg_latency_ms: float | None = None


class TopicCount(BaseModel):
    topic: str
    count: int


class DashboardResponse(BaseModel):
    period: Period
    kpis: KPIs
    top_searches: list[TopicCount]
    knowledge_gaps: list[TopicCount]
