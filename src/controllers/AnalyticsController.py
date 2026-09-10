from datetime import datetime

from .BaseController import BaseController


class AnalyticsController(BaseController):
    """Analytics Dashboard pipeline (2026-09-08, read-only). Turns
    IntentLogModel's own raw counts (get_dashboard_summary/
    get_topic_counts) into the exact KPI/rate shape routes/analytics.py
    returns — the repository owns every real SQL query against
    intent_log (claude.md §1.2's Ports & Adapters discipline: a
    controller never builds its own SQLAlchemy statement), this
    controller only owns the arithmetic and response shaping.

    Deliberately narrow rate semantics (see get_dashboard_summary's own
    docstring for the full reasoning this mirrors): answered_rate and
    not_found_rate are each counted independently against total_searches,
    never as `1 - the other` — that would silently fold
    escalated_verification/technical_error/clarification_requested/
    escalated_human/pending_clarification into whichever headline rate is
    computed as the complement, misrepresenting what actually happened on
    those turns."""

    def __init__(self, intent_log_model):
        super().__init__()
        self.intent_log_model = intent_log_model

    async def get_dashboard(self, client_id: str, date_from: datetime, date_to: datetime) -> dict:
        summary = await self.intent_log_model.get_dashboard_summary(client_id, date_from, date_to)
        total = summary["total_searches"]

        def _rate(count: int) -> float:
            # 2026-09-08 addendum: scaled to a 0-100 percentage (not a
            # 0-1 fraction) for direct dashboard UI presentation, rounded
            # to 2 decimal places -- e.g. 72.37, not 0.7236842105263158.
            # 0.0, not None/NaN, on a genuinely empty period — an empty
            # dashboard should read as "nothing happened", not error out
            # or divide by zero.
            return round((count / total) * 100, 2) if total else 0.0

        top_searches = await self.intent_log_model.get_topic_counts(
            client_id, date_from, date_to, outcome_status=None, limit=10,
        )
        knowledge_gaps = await self.intent_log_model.get_topic_counts(
            client_id, date_from, date_to, outcome_status="not_found", limit=10,
        )

        return {
            "period": {"period_from": date_from, "period_to": date_to},
            "kpis": {
                "total_searches": total,
                "answered_rate": _rate(summary["answered_count"]),
                "not_found_rate": _rate(summary["not_found_count"]),
                "escalated_verification_rate": _rate(summary["escalated_verification_count"]),
                "technical_error_rate": _rate(summary["technical_error_count"]),
                "clarification_requested_rate": _rate(summary["clarification_requested_count"]),
                "escalated_human_rate": _rate(summary["escalated_human_count"]),
                "pending_rate": _rate(summary["pending_clarification_count"]),
                # 2026-09-08 addendum -- see get_dashboard_summary's own
                # comment on human_handoff_offered_count for why this is a
                # separate, additive metric rather than a redefinition of
                # escalated_human_rate above: the union of every real
                # signal a human was offered/involved (not_found's own
                # handoff line, escalated_verification, and routing-level
                # escalated_human once that's ever reached).
                "human_handoff_offered_rate": _rate(summary["human_handoff_offered_count"]),
                "avg_latency_ms": summary["avg_latency_ms"],
            },
            "top_searches": top_searches,
            "knowledge_gaps": knowledge_gaps,
        }
