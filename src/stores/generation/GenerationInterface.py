from abc import ABC, abstractmethod


class GenerationTimeoutError(Exception):
    """Raised by a provider's generate_reply() when the underlying HTTP
    call to the model endpoint times out — a vendor-agnostic signal
    callers can catch without depending on `requests` (or any other HTTP
    client a future provider might use) leaking past the adapter
    boundary (claude.md §1.2). classify_intent() does NOT raise this: its
    own documented contract is to never raise at all, translating a
    timeout into 'unclassified' internally, the same as any other
    unparseable response — only generate_reply()'s failure needs to
    reach the caller, since only TextReplyController (not a provider)
    should decide what patient-facing text to show when generation
    itself is unavailable."""


class GenerationInterface(ABC):
    """Port every conversational-model adapter (QwenProvider today, a
    future alternative candidate tomorrow) must implement. Section 3
    controllers (TextReplyController, IntentRoutingController) only ever
    call these two methods against this interface — never a specific
    vendor SDK or a raw HTTP call inline (claude.md §1.2, §6.3) — so
    swapping the generation backend, or where it's hosted, is a single
    GENERATION_BACKEND / GENERATION_BASE_URL config change."""

    @abstractmethod
    async def generate_reply(
        self,
        messages: list[dict],
        temperature: float = 0.3,
        max_tokens: int = 512,
    ) -> str:
        """Chat-completion call. `messages` follows the standard
        {"role": "system"|"user"|"assistant", "content": str} shape.
        Returns the assistant's text content only — callers never see
        the raw provider response envelope.

        Raises GenerationTimeoutError if the underlying HTTP call times
        out — never swallowed here, since only the caller (not a vendor
        adapter) should decide what patient-facing fallback text to show."""
        pass

    @abstractmethod
    async def classify_intent(
        self,
        text: str,
        allowed_intents: list[str],
        history: list[dict] | None = None,
        guidance: str | None = None,
    ) -> str:
        """Zero-shot closed-set classification: returns exactly one value
        from `allowed_intents` (never a value outside that set — a
        provider that can't guarantee this maps an out-of-set response to
        the routing map's own 'unclassified'/fallback entry rather than
        raising). `allowed_intents` is supplied by the caller, sourced
        from utils/intent_routing_map.py — never hardcoded inside a
        provider (claude.md §1.3, §6.3).

        `history` (same {"role", "content"} shape as generate_reply's
        `messages`) is the recent conversation so far — required for a
        short reply like "اه" to classify correctly at all; classifying
        it in isolation has no way to know it's answering "do you want
        to book?" rather than being a stray word.

        `guidance` is optional, domain-specific classification instruction
        (real worked examples, edge-case rules) the caller resolves from a
        Bucket C directive — the generic "respond with this JSON shape"
        system prompt alone was observed to be too under-specified for
        real ambiguous cases (a broad "what services do you offer"
        question, an out-of-scope medical question); never hardcoded
        inside a provider, same reasoning as `allowed_intents`."""
        pass
