import logging

from .BaseController import BaseController
from stores.llm.templates.template_parser import TemplateParser, TemplateBucket
from stores.generation.GenerationInterface import GenerationTimeoutError

# Deliberately NOT a Bucket B template: prompt_templates.py's own header
# requires every entry there to be a verbatim transcription of real
# source business documents (claude.md §3.5) — this message describes no
# business fact, policy, or script, it's a generic technical-outage
# notice for when the generation backend itself doesn't respond in time.
# Forcing it into Bucket B would mean inventing "real source text" that
# was never transcribed from anywhere, which is worse than keeping it
# here as a small, explicitly-labeled, narrowly-scoped exception — the
# same reasoning claude.md §1.3 already applies to Bucket B/C's own
# hardcoding carve-out, just for a genuinely different (ops, not
# business-content) category of string.
GENERATION_UNAVAILABLE_FALLBACK = (
    "معذرة، في تأخير مؤقت في الرد دلوقتي. ممكن تجرب تاني بعد شوية؟"
)


class TextReplyController(BaseController):
    """Phase 1's dual-mode reply logic (Implementation Plan Step 1).
    Mode A: grounded, retrieval-scaled rewrite — single-chunk for narrow
    queries, multi-chunk synthesis for broad ones, always in Egyptian
    Arabic, always closing with a dynamic follow-up question. Mode B:
    verbatim Bucket B/C template substitution, zero LLM involvement.
    Never invents a fact, never paraphrases content the business
    requires exact wording for (claude.md §6.3).

    Language/dialect/cleanliness enforcement is entirely prompt-driven —
    whatsapp_mode_a_reply_directive and whatsapp_out_of_domain_directive
    (both Bucket C) carry the rules, enforced in a single generation pass.
    A prior version of this controller enforced an Arabic-only rule via
    _LATIN_LETTER_RE/_GULF_DIALECT_WORD_RE — removed deliberately: it had
    no way to distinguish "English word that should have been Arabic"
    from "English medical term (MRI, CT, CBC...) that must stay English",
    which is a real, structural problem for a medical domain, not a
    tuning issue. No hardcoded *business-content* fallback strings live
    here — every reply is either a resolved Bucket B/C template or live
    model output, with exactly one deliberate exception:
    GENERATION_UNAVAILABLE_FALLBACK, a generic technical-outage notice
    used only when generate_reply() itself times out (see Phase 0's
    infra hardening). That string describes no business fact or policy,
    so it doesn't belong in Bucket B's real-source-only content, and it's
    the one case where there's no model output to fall back on at all.

    An LLM-as-judge self-review pass (a second generate_reply call asking
    the model to re-check its own draft) was tried and removed again: on
    real test traffic it took a factually correct draft — "فرع سليمان
    أباظة: اسانسير متاح" — and confidently rewrote it into the opposite
    fact. Root cause was structural, not a prompt-wording issue: the
    review call's messages contained the system prompt and the draft
    text only, never the original CONTEXT block or the patient's
    question — so the "reviewer" had no grounding to verify a fact
    against and was really just free-associating a rewrite. It also
    doubled this method's LLM round-trips (already 2: breadth
    classification + generation) on an already-slow remote quantized
    model. A grounding-blind review pass is strictly worse than no
    review pass, so this controller now does exactly one generation call
    per reply and trusts it directly — see whatsapp_mode_a_reply_directive
    rule 1 for the current grounding/anti-fabrication wording.

    Structured field preservation: narrow-breadth CONTEXT is no longer
    the whole flattened chunk. ChunkingController now also stores each
    chunk's field-level {label: value} dict (metadata_payload["field_data"]),
    and field_selection_controller narrows that down to just the 1-2
    fields whose label actually matches the patient's question (embedding
    cosine similarity, not a keyword/regex match — see
    FieldSelectionController). This targets the largest documented
    failure category head-on: the model no longer has to silently ignore
    13 unrelated fields from memory while phrasing an answer, because it's
    never shown them. Falls back to full chunk content — today's
    behavior — whenever field_data is empty/missing (every chunk synced
    before this change) or nothing clears the similarity floor, so this
    is additive and never regresses a chunk that hasn't been re-synced
    yet.

    A later round also tried a strict "review every affirmation/negation
    word-by-word against CONTEXT" instruction plus isolated single-word
    ❌/✅ examples (e.g. a bare 'إزاي') in that same directive, paired with
    repetition_penalty=1.1 at temperature=0.1. On real traffic this
    combination suffocated the model rather than steadying it: robotic
    copy-pasted chunks, the example word forced into replies where it
    didn't belong, and outright gibberish tokens (an English word like
    "Zombies" appearing mid-Arabic-sentence) — repetition_penalty pushing
    a near-greedy quantized model into unmapped low-probability territory
    to avoid reusing ordinary Arabic function words, not just varying
    phrasing. Both were reverted: no repetition_penalty, temperature
    raised to 0.2 for a little recovery margin, and the directive rewritten
    around natural full-sentence examples instead of a literal checklist."""

    def __init__(
        self,
        retrieval_controller,
        client_config_model,
        generation_client,
        dialogue_state_template_map_model,
        field_selection_controller,
    ):
        super().__init__()
        self.retrieval_controller = retrieval_controller
        self.client_config_model = client_config_model
        self.generation_client = generation_client
        self.dialogue_state_template_map_model = dialogue_state_template_map_model
        self.field_selection_controller = field_selection_controller
        self.template_parser = TemplateParser()
        self.logger = logging.getLogger(__name__)

    async def reply(self, client_id: str, text: str, session_state: dict) -> tuple[str, str]:
        """Returns (reply_text, mode) — mode is "mode_a" or "mode_b",
        surfaced for logging/Postman verification, never used by the
        caller to branch (the decision already happened here)."""
        dialogue_state = session_state.get("dialogue_state")
        if dialogue_state:
            mapping = await self.dialogue_state_template_map_model.get_template_for_state(client_id, dialogue_state)
            if mapping is not None:
                bucket = TemplateBucket.B if mapping.bucket == "B" else TemplateBucket.C
                substitutions = self._mode_b_substitutions(session_state)
                return self.template_parser.resolve(bucket, mapping.template_id, **substitutions), "mode_b"

        return await self._mode_a_reply(client_id, text, session_state), "mode_a"

    def _mode_b_substitutions(self, session_state: dict) -> dict:
        """Real, sensible values for the placeholders the call-script-derived
        Bucket B templates carry over from their original human-agent source
        (claude.md §3.5) — $brand, $employee_name, etc. Passing every known
        key unconditionally is safe: string.Template.substitute() only
        consumes the ones an individual template actually references and
        ignores the rest, so this doesn't need to know which template it's
        about to render."""
        brand_filter = session_state.get("brand_filter")
        brand_label = {"cairoscan": "كايروسكان", "technoscan": "تكنوسكان"}.get(brand_filter, "رايلاب")
        return {
            "brand": brand_label,
            # No literal human agent exists in an AI-driven WhatsApp flow —
            # the original call-script source's $employee_name slot is
            # filled with the AI assistant's own identity instead.
            "employee_name": "مساعد رايلاب الذكي",
        }

    async def _narrow_context_block(self, text: str, results: list[dict], client_config) -> str:
        """Builds narrow-breadth CONTEXT one already-retrieved chunk at a
        time: hand field_selection_controller that chunk's structured
        field_data and keep only the label:value pairs it says actually
        match the question, instead of the chunk's full flattened text.
        Loops over every chunk in `results` rather than assuming exactly
        one — correct either way, since whatsapp_retrieval_top_k_narrow
        is itself a client_config value, never assumed to be 1 in code.

        Two independent fallbacks to the chunk's full content, both real
        and both expected in production, not error cases: (a) field_data
        is empty/missing — true for every chunk synced before this
        change existed, self-resolving as clients re-sync; (b) field_data
        exists but nothing cleared the similarity floor for this
        particular question — safer to hand over the whole chunk than to
        silently under-inform the model with an empty CONTEXT."""
        context_lines = []
        for result in results:
            chunk = result["chunk"]
            field_data = (chunk.metadata_payload or {}).get("field_data")

            selected_fields = {}
            if field_data:
                selected_fields = await self.field_selection_controller.select_relevant_fields(
                    text,
                    field_data,
                    min_similarity=client_config.whatsapp_min_relevance_score,
                    max_fields=client_config.whatsapp_field_selection_max_fields,
                )

            if selected_fields:
                context_lines.extend(f"{label}: {value}" for label, value in selected_fields.items())
            else:
                context_lines.append(chunk.content)

        return "\n".join(context_lines)

    def _classify_breadth(self, text: str, results: list[dict], client_config) -> str:
        """Deterministic replacement for the old classify_intent(["narrow",
        "broad"]) LLM call — the reranker's own score distribution over
        `results` decides breadth, algorithmically, no LLM autonomy over
        this decision. `results` is the SAME real, already-retrieved set
        being routed, never a separate lookup — this can't disagree with
        what was actually found. `text` is only used for logging/tracing
        here — the classification itself depends solely on `results`'
        real scores, never on the query string.

        Narrow requires two AND'd conditions, both real client_config
        values (claude.md §1.3 — a similarity/score cutoff is a business
        threshold, never a Python literal): the top result's score clears
        whatsapp_breadth_score_threshold, AND the drop-off to the second
        result clears whatsapp_breadth_score_gap. Fewer than 2 results
        means there's nothing to be broad about — narrow by construction.
        Zero results defers entirely to the existing zero-chunk handling
        in _mode_a_reply; this method still returns a label for that case
        but nothing downstream uses it (no results to slice or format)."""
        if len(results) < 2:
            decision = "narrow"
            self.logger.info(
                f"[breadth] query={text!r} decision={decision!r} reason='fewer than 2 results "
                f"({len(results)})'"
            )
            return decision

        scores = [r["score"] for r in results]
        top_score = scores[0]
        gap = top_score - scores[1]
        threshold = client_config.whatsapp_breadth_score_threshold
        gap_threshold = client_config.whatsapp_breadth_score_gap

        decision = (
            "narrow" if top_score >= threshold and gap >= gap_threshold else "broad"
        )

        # DEBUG: the full raw score list — verbose, only needed when
        # actually tracing a specific misclassification.
        self.logger.debug(f"[breadth] query={text!r} all_scores={scores}")
        # INFO: the decision and the exact numbers that produced it —
        # always worth having, this is the business decision itself.
        self.logger.info(
            f"[breadth] query={text!r} top_score={top_score:.4f} gap={gap:.4f} "
            f"threshold={threshold} gap_threshold={gap_threshold} decision={decision!r}"
        )
        return decision

    async def _mode_a_reply(self, client_id: str, text: str, session_state: dict) -> str:
        history = session_state.get("history", [])
        client_config = await self.client_config_model.get_client_config(client_id)

        brand_filter = session_state.get("brand_filter")
        metadata_filters = None
        if brand_filter and "Account" in (client_config.allowed_metadata_keys or []):
            metadata_filters = {"Account": brand_filter}

        # Retrieve at the broad ceiling unconditionally, first — breadth
        # is now decided FROM these results' own score distribution, not
        # before retrieval runs (see _classify_breadth). This replaces a
        # second classify_intent(["narrow","broad"]) LLM call that real
        # traffic proved unreliable: literal [BEGIN SOURCE] scaffolding
        # was leaking into patient-facing replies for questions that were
        # unambiguously narrow, because that wrapper only exists on the
        # broad-path branch below — proof the old classifier was routing
        # narrow questions there, which also meant FieldSelectionController
        # never ran on those turns at all.
        broad_top_k = client_config.whatsapp_retrieval_top_k_broad
        results = await self.retrieval_controller.retrieve(
            client_id=client_id, query=text, metadata_filters=metadata_filters, top_k_override=broad_top_k,
        )

        cross_brand_note = None
        if not results and metadata_filters:
            # Cross-brand suggestion: before declining, check whether the
            # service exists under the sibling brand and say so plainly,
            # grounded in the real, already-transcribed cross-referral
            # directive rather than an invented instruction.
            unfiltered_results = await self.retrieval_controller.retrieve(
                client_id=client_id, query=text, metadata_filters=None, top_k_override=broad_top_k,
            )
            if unfiltered_results:
                results = unfiltered_results
                cross_brand_note = self.template_parser.resolve(
                    TemplateBucket.C, "directive_brand_cross_referral",
                )

        breadth = self._classify_breadth(text, results, client_config)
        if breadth == "narrow":
            # Slice down to the narrow ceiling only now that breadth is
            # decided — never re-retrieved, just the same real results
            # trimmed, so this can't disagree with what was just scored.
            results = results[:client_config.whatsapp_retrieval_top_k_narrow]

        # No relevance-score gate here (tried and removed): real traffic
        # showed it false-positiving on genuinely in-domain narrow
        # queries (exam prep, pricing) whose top score happened to land
        # just under the threshold, even though results[0] was
        # consistently the correct chunk regardless of its numeric score.
        # The retrieved top chunk is trusted unconditionally — the only
        # thing that routes a turn to the out-of-domain path now is
        # retrieval legitimately returning zero chunks.
        if not results:
            # Zero retrieved chunks: the out-of-domain directive is
            # deliberately given NO context to draw from, so the model has
            # nothing to fabricate a fact from — it can only acknowledge
            # the request and redirect.
            self.logger.info(f"Mode A: zero retrieved chunks for {text!r}, generating dynamic out-of-domain decline")
            system_prompt = self.template_parser.resolve(TemplateBucket.C, "whatsapp_out_of_domain_directive")
            messages = [
                {"role": "system", "content": system_prompt},
                *history,
                {"role": "user", "content": f"PATIENT MESSAGE:\n{text}"},
            ]
            # Out-of-domain replies are always 1-2 sentences by directive,
            # regardless of the original query's breadth classification.
            # Tighter than the narrow/broad grounded budgets below (100/
            # 250): real traffic showed the extra headroom past where a
            # correct 1-2 sentence decline naturally ends was exactly
            # where this specific call drifted into Chinese-script tokens
            # — a short apology never needed 100 tokens to begin with.
            max_tokens = 60
        else:
            system_prompt = self.template_parser.resolve(TemplateBucket.C, "whatsapp_mode_a_reply_directive")

            if breadth == "narrow":
                context_block = await self._narrow_context_block(text, results, client_config)
            else:
                # Broad queries keep full multi-chunk content — Rule 5
                # explicitly wants breadth here (a summary across several
                # services), so narrowing down to "the most relevant
                # field" per chunk would work against the task, not for
                # it. Each chunk gets explicit structural delimiters —
                # real testing showed the model misattributing a
                # qualifier from one chunk to an entity named in a
                # different one during broad-query synthesis, when
                # chunks were separated only by a bullet "-" and each
                # chunk's own embedded "[Document: ...]" prefix. A
                # numbered [BEGIN SOURCE n]/[END SOURCE n] wrapper gives
                # the model an explicit, unambiguous boundary to keep
                # facts scoped to their actual source, without
                # hardcoding any content.
                context_block = "\n\n".join(
                    f"[BEGIN SOURCE {index}]\n{result['chunk'].content}\n[END SOURCE {index}]"
                    for index, result in enumerate(results, start=1)
                )

            # INFO: a summary safe to always have on hand (breadth, chunk
            # count, size) without paying the log-file-size or
            # patient-data-verbosity cost of the full text on every turn.
            self.logger.info(
                f"[context] query={text!r} breadth={breadth!r} chunk_count={len(results)} "
                f"context_chars={len(context_block)}"
            )
            # DEBUG: the exact final CONTEXT string handed to the LLM —
            # this is real patient-adjacent business content, so it's
            # opt-in verbosity (RAG_LOG_LEVEL=DEBUG), not logged by default.
            self.logger.debug(f"[context] query={text!r} full_context_block=\n{context_block}")

            user_sections = [f"CONTEXT:\n{context_block}"]
            if cross_brand_note:
                user_sections.append(
                    "NOTE — the above context was found under the OTHER brand than the "
                    f"patient's active filter. Apply this rule when phrasing your reply: {cross_brand_note}"
                )
            user_sections.append(f"PATIENT MESSAGE:\n{text}")

            messages = [
                {"role": "system", "content": system_prompt},
                *history,
                {"role": "user", "content": "\n\n".join(user_sections)},
            ]

            # Breadth-aware budget: a narrow, 1-2-sentence answer (Rule 1)
            # needs far less room than a broad multi-chunk summary (Rule
            # 5) — and real test traffic showed the model's failures
            # (fabricated add-ons, language drift) consistently landing in
            # the *unused tail* of an overly generous fixed budget, past
            # where a correct answer had already finished.
            max_tokens = 250 if breadth == "broad" else 100

        # temperature history on this call: 0.1 (too greedy — got stuck in
        # loops when combined with repetition_penalty=1.1, since reverted),
        # then 0.2 (fixed that, but loosened things enough that the model
        # started hallucinating numbers/timestamps instead of copying them
        # from CONTEXT). 0.15 splits the difference — close enough to
        # deterministic for a grounded extraction task to keep numeric
        # fidelity, with enough margin to not re-trigger the 0.1 lockup
        # now that a (much smaller) repetition_penalty is back too.
        try:
            return await self.generation_client.generate_reply(messages, temperature=0.15, max_tokens=max_tokens)
        except GenerationTimeoutError as e:
            # Root-caused against real Falcon-H1 golden-suite traffic
            # (Phase 0 bake-off): a slow/eager-mode remote deployment can
            # legitimately exceed the request timeout. This is the one
            # call site allowed to return non-Bucket-B, non-model text —
            # see GENERATION_UNAVAILABLE_FALLBACK's own comment for why.
            self.logger.warning(f"Mode A: generation timed out for {text!r} ({e}) — returning fallback reply")
            return GENERATION_UNAVAILABLE_FALLBACK
