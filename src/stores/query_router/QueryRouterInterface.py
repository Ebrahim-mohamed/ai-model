from abc import ABC, abstractmethod


class QueryRouterInterface(ABC):
    """Port the dedicated query-rewriting/intent-classification sidecar
    must implement (Section 3 Step 1, 2026-08-30 architecture pivot — see
    README's own history for why this replaced both the earlier single-
    generation-client classify_intent() call and the deterministic
    last_topic_hint mechanism it was tried alongside). Deliberately
    separate from GenerationInterface: this sidecar's only job is
    classify + rewrite, never grounded reply generation, so its own
    interface never grows a generate_reply()-shaped method — a narrow,
    honest contract instead of bolting a second responsibility onto the
    existing generation port (claude.md §1.2's Ports & Adapters
    discipline: a new external dependency with a genuinely different
    responsibility gets its own Interface/Enums/ProviderFactory/providers/
    set, not a reused one).

    2026-08-31 (task split): classify_intent() and rewrite_query() were
    ONE combined classify_and_rewrite() call sharing a single system
    prompt until real traffic surfaced a structural conflict — Nile-Chat-
    4B's real 2048-token context forced that shared prompt to stay terse,
    which measurably weakened intent-classification rule-following, while
    the worked examples needed to teach reliable anaphora resolution were
    shown (also from real traffic — a resolved_query that echoed a
    literal exam name straight out of the prompt's own example instead of
    the real conversation) to risk the model copying their literal
    vocabulary instead of generalizing the pattern. Splitting into two
    focused calls gives each task its own full context budget, so each
    guidance can be as verbose/example-rich as it actually needs without
    competing with the other's token cost.

    2026-09-01 (Contextual Query Reformulation reordering): even split,
    with a fully abstracted rewrite guidance, the SAME anaphora-
    resolution failure kept recurring in real traffic — a third distinct
    prompt-engineering attempt still failed to reliably resolve a generic
    follow-up ("الفحص"/"ـه") to a name established earlier in the
    conversation. Root cause identified as ordering, not wording:
    classify_intent() was still being asked to reason about history/
    pronouns on the SAME turn its own routing decision was being made.
    IntentRoutingController now calls rewrite_query() FIRST, always,
    unconditionally — before intent is even known — and passes ITS
    output to classify_intent() as `text`, never the raw patient message.
    classify_intent() therefore never needs pronoun/history-resolution
    instructions of its own anymore; its only job is labeling an
    already-standalone query. Trade-off accepted deliberately: this
    removes the "skip rewrite_query() for non-RAG intents" efficiency
    gain the split briefly had, since routing can no longer be decided
    before rewriting happens — every turn now costs two calls, not
    conditionally one or two.

    2026-09-01 (model retirement — NileChat4BProvider deleted entirely):
    even after the reordering above, the exact same generic-reference
    anaphora-resolution failure persisted — a FOURTH distinct attempt.
    Temporary raw-response logging added to rewrite_query() confirmed
    this was a genuine reasoning failure, not a parsing bug: the model
    returned perfectly valid JSON, with `resolved_query` populated but
    literally identical to the unresolved raw input, no fallback path
    triggered. Four separate prompt-engineering strategies failing the
    same narrow skill, while every other capability (intent
    classification, verbatim preservation, resolution given a strong
    enough literal signal) worked reliably, was treated as real evidence
    of a reliability ceiling specific to that 4B checkpoint's capability
    on this one task — not something a fifth prompt rewrite was likely to
    fix. NileChat4BProvider was deleted outright (not kept as a fallback
    option) and replaced by NileChat12BBaseProvider — the RAW, unadapted
    MBZUAI-Paris/Nile-Chat-12B checkpoint Mode A's own fine-tuned
    generation client was trained FROM, used here in its general-purpose
    form rather than that fine-tuned, narrowly-collapsed shape. This is a
    genuine architectural bet backed by general model-scaling trends, not
    a proven fix — same "strong evidence-informed bet" status the 4B
    provider itself started with, pending real multi-turn Postman
    verification."""

    @abstractmethod
    async def classify_intent(
        self,
        text: str,
        history: list[dict],
        allowed_intents: list[str],
        guidance: str | None = None,
    ) -> str:
        """Returns `intent` — never raises, under any failure (timeout,
        connection failure, malformed/unparseable JSON, an out-of-set
        value): a provider that can't confidently classify must degrade
        to `"inquiry"` (2026-08-31: the closed three-value set —
        complaint/inquiry/book_appointment, utils/intent_routing_map.py —
        dropped the old, never-a-real-model-output "unclassified" label;
        `inquiry` is the real, always-in-the-set catch-all that already
        routes to TEXT_PIPELINE). This call sits in front of every single
        inbound turn, so a provider must never let an implementation
        detail (a dropped connection, a malformed response) become a
        crashed turn.

        `intent` is exactly one value from `allowed_intents` — sourced by
        the caller from utils/intent_routing_map.py, never hardcoded
        inside a provider (claude.md §1.3).

        2026-09-02 (dynamic metadata pre-filtering — added, then reverted
        same day): this method briefly also predicted a `target_sheet`
        for retrieval pre-filtering. Reverted after real production
        evidence: the two-field task measurably destabilized the model —
        repetition-loop failures (echoing the patient's message back
        verbatim instead of ever emitting JSON) and systematic wrong-sheet
        guesses, with the sheet-filter feature never once producing a
        correct accepted result in real traffic. Removed outright rather
        than patched — see IntentRoutingController/TextReplyController's
        own git history for the matching removal on the caller side.

        `text` (2026-09-01, CQR reordering): the caller passes
        rewrite_query()'s OWN output here, never the patient's raw
        message — by the time this call runs, references/pronouns are
        already resolved, so classification never has to reason about
        history itself.

        `history` ({"role", "content"} shape, same as GenerationInterface.
        generate_reply's `messages`) is the real, hydrated conversation so
        far — passed through only as light supporting context (e.g.
        overall tone for a possible complaint), never something this call
        needs to actively resolve anything against anymore.

        `guidance` is optional, domain-specific instruction (real worked
        examples, edge-case disambiguation rules) the caller resolves from
        a Bucket C directive — never hardcoded inside a provider, same
        reasoning as `allowed_intents`."""
        pass

    @abstractmethod
    async def rewrite_query(
        self,
        text: str,
        history: list[dict],
        guidance: str | None = None,
    ) -> str:
        """Returns `resolved_query` — never raises, under any failure:
        degrades to the original, unmodified `text` (reproduces pre-
        sidecar retrieval behavior exactly — never blocks a turn on this
        call failing).

        A short, self-contained, standalone rewrite of `text` suitable
        for retrieval on its own — resolving pronouns, short/ambiguous
        replies ("اه" after the assistant asked a specific question), and
        topic continuations using `history`. If `text` is already a
        complete, standalone question, resolved_query is that same text
        unchanged — never a rewrite for its own sake. This value is used
        ONLY for retrieval; the original `text` remains what reaches the
        model as the patient's own message, and what gets recorded in
        chat_history/human_handoff_queue — see TextReplyController.reply's
        own docstring for the full contract.

        Called FIRST, unconditionally, on every turn (2026-09-01) —
        before intent is even known, since classify_intent() now depends
        on this call's output rather than the other way around. No longer
        skipped for non-RAG intents (see this interface's own class-level
        docstring for the efficiency trade-off this accepts).

        `history` — same shape/contract as classify_intent's own `history`
        parameter.

        `guidance` — same sourcing contract as classify_intent's own
        `guidance` parameter, but resolved from a SEPARATE Bucket C
        directive (`whatsapp_query_rewrite_guidance`, not
        `whatsapp_intent_classification_guidance`) — the two tasks no
        longer share a prompt, so they don't share guidance content
        either. Real production evidence: any worked example here MUST
        use abstract placeholders (e.g. `[EXAM_NAME]`, `[ASPECT_ASKED]`),
        never concrete Arabic vocabulary — the model was observed copying
        a literal exam name straight out of a concrete example into a
        real conversation that never mentioned it."""
        pass
