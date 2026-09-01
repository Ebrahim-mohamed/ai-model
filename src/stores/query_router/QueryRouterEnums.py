from enum import Enum


class QueryRouterEnums(Enum):
    """Closed set of query-router/intent-classification sidecar adapters
    — mapped to a concrete class only inside QueryRouterProviderFactory
    (claude.md §1.2). A future lightweight candidate is a new value here
    plus a new providers/ file, never a rewrite of this set or of any
    controller that depends on QueryRouterInterface.

    2026-09-01: NILE_CHAT_4B removed entirely, not just superseded —
    real production evidence (four separate prompt-engineering attempts,
    all failing the same generic-reference anaphora-resolution pattern;
    see QueryRouterInterface's own docstring for the full history) showed
    this specific 4B checkpoint hitting a genuine reliability ceiling on
    this task, not a prompting problem. NILE_CHAT_12B_BASE replaces it:
    the same MBZUAI-Paris/Nile-Chat-12B checkpoint Mode A's own
    generation client was fine-tuned FROM, used here in its raw,
    unadapted form — general-purpose, not narrowly LoRA-collapsed into
    Mode A's own JSON-then-phrasing shape the way that fine-tuned sibling
    is (stores/generation/providers/NileChatProvider.py)."""

    NILE_CHAT_12B_BASE = "NILE_CHAT_12B_BASE"
