from datetime import datetime

from sqlalchemy import select, func, or_

from .BaseDataModel import BaseDataModel
from .db_schemes.raylab.schemes import IntentLog


class RoutingOutcome:
    """Closed set for IntentLog.routing_outcome — plain string values,
    same convention as MessageDirection. AI_HANDLED, ESCALATED_HUMAN, and
    PENDING_CLARIFICATION all count as the system working correctly
    (claude.md §6 / Phase 6's dashboard design) — none of these are
    'failures' to filter out of analytics."""

    AI_HANDLED = "ai_handled"
    ESCALATED_HUMAN = "escalated_human"
    PENDING_CLARIFICATION = "pending_clarification"
    NOT_IMPLEMENTED = "not_implemented"  # Steps 2-5 phases not yet built


class IntentLogModel(BaseDataModel):
    """Repository for intent_log — Phase 6's analytics log. Every method
    requires client_id, no exceptions. The table itself is append-only,
    written from exactly one call site (IntentRoutingController, via the
    fire-and-forget tasks.log_intent Celery task — see log_turn). The
    read/aggregate methods below (2026-09-08, Analytics Dashboard
    pipeline) are a second, read-only call site — AnalyticsController —
    that never writes; this repository still owns every real SQL query
    against this table, so no other file re-implements one."""

    def __init__(self, db_client: object):
        super().__init__(db_client=db_client)

    @classmethod
    async def create_instance(cls, db_client: object):
        return cls(db_client)

    async def log_turn(
        self,
        client_id: str,
        session_id,
        modality: str,
        intent: str,
        routing_outcome: str,
        resolution_status: str | None = None,
        outcome_status: str | None = None,
        extracted_topic: str | None = None,
        retrieval_score: float | None = None,
        latency_ms: float | None = None,
    ) -> IntentLog:
        entry = IntentLog(
            client_id=client_id,
            session_id=session_id,
            modality=modality,
            intent=intent,
            routing_outcome=routing_outcome,
            resolution_status=resolution_status,
            outcome_status=outcome_status,
            extracted_topic=extracted_topic,
            retrieval_score=retrieval_score,
            latency_ms=latency_ms,
        )
        async with self.db_client() as session:
            async with session.begin():
                session.add(entry)
            await session.commit()
            await session.refresh(entry)
        return entry

    async def get_dashboard_summary(self, client_id: str, date_from: datetime, date_to: datetime) -> dict:
        """Analytics Dashboard pipeline (2026-09-08) — one round trip,
        conditional-aggregate counts (`func.count(...).filter(...)`,
        `func.avg(...).filter(...)`) rather than N separate scalar
        queries, since every count/average here shares the same
        client_id + date-range base filter. total_searches counts every
        row in range regardless of outcome_status/routing_outcome — see
        this repository's own callers (AnalyticsController) for how the
        individual counts below are turned into rates against that total.

        answered/not_found/escalated_verification/technical_error read
        `outcome_status` (TextReplyController._mode_a_reply's own real,
        structural determination — see that method's docstring); NULL for
        any row that never reached a Mode A search attempt (Mode B, or a
        not-yet-implemented routing target), so those rows never
        contribute to any of these four counts, only to total_searches.
        escalated_human/pending_clarification instead read the existing
        `routing_outcome` column — a routing-level concept, orthogonal to
        outcome_status (see IntentLog's own column comments) — currently
        always 0 in real data since RoutingTarget's complaint/booking
        targets aren't implemented yet (IntentRoutingController.route_turn
        only ever writes AI_HANDLED or NOT_IMPLEMENTED today), not a bug
        in this query."""
        base_filter = (
            IntentLog.client_id == client_id,
            IntentLog.created_at >= date_from,
            IntentLog.created_at <= date_to,
        )
        stmt = select(
            func.count().label("total_searches"),
            func.count().filter(IntentLog.outcome_status == "answered").label("answered_count"),
            func.count().filter(IntentLog.outcome_status == "not_found").label("not_found_count"),
            func.count().filter(IntentLog.outcome_status == "escalated_verification").label("escalated_verification_count"),
            func.count().filter(IntentLog.outcome_status == "technical_error").label("technical_error_count"),
            # 2026-09-08, Dynamic Disambiguation/Clarification Flow — reads
            # outcome_status, deliberately NOT the same thing as
            # pending_clarification_count below (which reads the pre-
            # existing routing_outcome column, for the unbuilt complaint/
            # booking routing targets). A Mode A turn that asked the
            # patient to pick a real sub-variant is a different concept
            # from a turn routed away from Mode A entirely.
            func.count().filter(IntentLog.outcome_status == "clarification_requested").label("clarification_requested_count"),
            func.count().filter(IntentLog.routing_outcome == RoutingOutcome.ESCALATED_HUMAN).label("escalated_human_count"),
            func.count().filter(IntentLog.routing_outcome == RoutingOutcome.PENDING_CLARIFICATION).label("pending_clarification_count"),
            # 2026-09-08 addendum, real incident: a dashboard viewer read
            # escalated_human_rate=0.0 as "the system never hands off to a
            # human," when 6 real not_found turns actually DID close with a
            # human-handoff offer ("حابب أحولك لحد من الفريق...") -- just
            # logged under outcome_status='not_found' (a Mode-A "this exam
            # doesn't exist" concept), not routing_outcome='escalated_human'
            # (a routing-level concept for the not-yet-built complaint/
            # booking flow). Rather than conflate the two existing, already-
            # validated buckets, this is a THIRD, additive count: every real
            # signal that a human was offered or involved in this turn --
            # not_found's own deterministic handoff line, escalated_
            # verification (ReplyVerificationController's own real handoff,
            # via human_handoff_queue), or (once it's ever actually reached)
            # routing-level escalated_human. Currently numerically equal to
            # not_found_count alone, since the other two are still always 0
            # in real traffic -- this becomes a genuinely different, wider
            # number the moment either of those starts firing for real.
            func.count().filter(
                or_(
                    IntentLog.outcome_status.in_(["not_found", "escalated_verification"]),
                    IntentLog.routing_outcome == RoutingOutcome.ESCALATED_HUMAN,
                )
            ).label("human_handoff_offered_count"),
            func.avg(IntentLog.latency_ms).label("avg_latency_ms"),
        ).where(*base_filter)

        async with self.db_client() as session:
            result = await session.execute(stmt)
            row = result.one()

        return {
            "total_searches": row.total_searches,
            "answered_count": row.answered_count,
            "not_found_count": row.not_found_count,
            "escalated_verification_count": row.escalated_verification_count,
            "technical_error_count": row.technical_error_count,
            "clarification_requested_count": row.clarification_requested_count,
            "escalated_human_count": row.escalated_human_count,
            "pending_clarification_count": row.pending_clarification_count,
            "human_handoff_offered_count": row.human_handoff_offered_count,
            "avg_latency_ms": float(row.avg_latency_ms) if row.avg_latency_ms is not None else None,
        }

    async def get_topic_counts(
        self,
        client_id: str,
        date_from: datetime,
        date_to: datetime,
        outcome_status: str | None = None,
        limit: int = 10,
    ) -> list[dict]:
        """Analytics Dashboard pipeline (2026-09-08) — shared by both
        "Top Searches" (outcome_status=None — every real search attempt,
        any outcome) and "Knowledge Gaps" (outcome_status="not_found") —
        same query shape, the only difference is one extra WHERE clause,
        so this is one method, not two. Always excludes NULL
        extracted_topic (a row that never reached classify_topic at all —
        Mode B/not-yet-implemented turns, or a task still in flight when
        this is queried) — grouping NULL together with a real
        "unclassified" classification would conflate two structurally
        different things: "never classified" vs. "classified as not
        matching any known taxonomy entry", the latter of which is itself
        useful signal (see client_config.topic_taxonomy's own column
        comment)."""
        conditions = [
            IntentLog.client_id == client_id,
            IntentLog.created_at >= date_from,
            IntentLog.created_at <= date_to,
            IntentLog.extracted_topic.isnot(None),
        ]
        if outcome_status is not None:
            conditions.append(IntentLog.outcome_status == outcome_status)

        stmt = (
            select(IntentLog.extracted_topic, func.count().label("count"))
            .where(*conditions)
            .group_by(IntentLog.extracted_topic)
            .order_by(func.count().desc())
            .limit(limit)
        )

        async with self.db_client() as session:
            result = await session.execute(stmt)
            rows = result.all()

        return [{"topic": row.extracted_topic, "count": row.count} for row in rows]
