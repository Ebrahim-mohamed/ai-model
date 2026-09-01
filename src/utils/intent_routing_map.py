from enum import Enum


class Intent(Enum):
    """The closed intent taxonomy — matches the Implementation Plan's own
    routing table exactly. New intents are added here (one enum value)
    plus one entry in INTENT_ROUTING_MAP below, never as a new `if`
    branch in IntentRoutingController (claude.md §1.3, §6.3).

    2026-08-31: deliberately narrowed to exactly three categories
    (complaint / inquiry / book_appointment), replacing the earlier
    seven-value taxonomy (query_price/query_schedule/query_branch/
    general_inquiry collapsed into `inquiry`; `cancel_appointment`
    folded into `book_appointment` — a real booking action either way;
    `unclassified` removed — classify_intent's own failure fallback now
    returns `inquiry` directly, the same safe catch-all target, rather
    than routing through a fourth label that was never a real model
    output). A simpler, stricter closed set for the 4B query-router
    sidecar to classify against."""

    COMPLAINT = "complaint"
    INQUIRY = "inquiry"
    BOOK_APPOINTMENT = "book_appointment"


class RoutingTarget(Enum):
    """Which phase's controller a classified intent dispatches to."""

    TEXT_PIPELINE = "text_pipeline"            # Step 1 — implemented
    COMPLAINTS_PIPELINE = "complaints_pipeline"  # Step 2 — not yet built
    BOOKING_PIPELINE = "booking_pipeline"        # Step 5 — not yet built


# The data-driven intent -> target mapping (Implementation Plan §1.2 /
# claude.md §6.3): IntentRoutingController dispatches by looking this up,
# never by branching on the intent string itself. Extending this later —
# a seventh phase, a new intent — is a new entry here, not an edit to the
# controller's own logic.
INTENT_ROUTING_MAP: dict[str, str] = {
    Intent.COMPLAINT.value: RoutingTarget.COMPLAINTS_PIPELINE.value,
    Intent.BOOK_APPOINTMENT.value: RoutingTarget.BOOKING_PIPELINE.value,
    Intent.INQUIRY.value: RoutingTarget.TEXT_PIPELINE.value,
}


def get_routing_target(intent: str) -> str:
    # Same safe-default rationale the old UNCLASSIFIED entry documented:
    # Mode A's own "decline rather than guess" behavior already handles
    # "couldn't confidently classify this" gracefully, so an unrecognized
    # intent string still routes through the same grounded pipeline
    # rather than a second, less-tested fallback path.
    return INTENT_ROUTING_MAP.get(intent, RoutingTarget.TEXT_PIPELINE.value)


def get_allowed_intents() -> list[str]:
    """The closed label set handed to the query-router sidecar's
    classify_intent call — sourced from this one place, never re-typed
    at the call site."""
    return [intent.value for intent in Intent]
