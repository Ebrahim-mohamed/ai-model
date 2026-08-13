from enum import Enum


class Intent(Enum):
    """The closed intent taxonomy — matches the Implementation Plan's own
    routing table exactly. New intents are added here (one enum value)
    plus one entry in INTENT_ROUTING_MAP below, never as a new `if`
    branch in IntentRoutingController (claude.md §1.3, §6.3)."""

    COMPLAINT = "complaint"
    BOOK_APPOINTMENT = "book_appointment"
    CANCEL_APPOINTMENT = "cancel_appointment"
    QUERY_SCHEDULE = "query_schedule"
    QUERY_PRICE = "query_price"
    QUERY_BRANCH = "query_branch"
    GENERAL_INQUIRY = "general_inquiry"
    UNCLASSIFIED = "unclassified"


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
    Intent.CANCEL_APPOINTMENT.value: RoutingTarget.BOOKING_PIPELINE.value,
    Intent.QUERY_SCHEDULE.value: RoutingTarget.TEXT_PIPELINE.value,
    Intent.QUERY_PRICE.value: RoutingTarget.TEXT_PIPELINE.value,
    Intent.QUERY_BRANCH.value: RoutingTarget.TEXT_PIPELINE.value,
    Intent.GENERAL_INQUIRY.value: RoutingTarget.TEXT_PIPELINE.value,
    # Mode A's own "decline rather than guess" behavior already handles
    # "couldn't find a real answer" gracefully — routing an unclassified
    # message through the same grounded pipeline is safer than inventing
    # a second, less-tested fallback path for this one case.
    Intent.UNCLASSIFIED.value: RoutingTarget.TEXT_PIPELINE.value,
}


def get_routing_target(intent: str) -> str:
    return INTENT_ROUTING_MAP.get(intent, RoutingTarget.TEXT_PIPELINE.value)


def get_allowed_intents() -> list[str]:
    """The closed label set handed to GenerationInterface.classify_intent
    — sourced from this one place, never re-typed at the call site."""
    return [intent.value for intent in Intent]
