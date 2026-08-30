import json
import logging
import re

from .BaseController import BaseController
from stores.llm.templates.template_parser import TemplateParser, TemplateBucket
from stores.generation.GenerationInterface import GenerationTimeoutError

# Matches the fenced ```json ... ``` block the fine-tuned "JSON-then-
# phrasing" discipline trains the model to lead every Mode A reply with
# (see scripts/finetune_data/teacher.py's own GENERATE_QUESTION_TASK
# contract, which this production shape mirrors). Same extraction
# pattern NileChatProvider.classify_intent already uses for its own
# {"intent": ...} block — kept consistent rather than inventing a
# second regex convention for the same kind of problem in this codebase.
_JSON_BLOCK_RE = re.compile(r"```json\s*(.*?)\s*```", re.DOTALL)

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


def _split_json_and_phrasing(raw_output: str) -> tuple[object | None, str]:
    """Splits a Mode A generation's raw text into (parsed_json_block,
    phrasing) — the same two-part shape every training example in
    finetune_data_out/train.json was built around. Returns
    (parsed_json_or_None, phrasing_text), never raises: this is a
    best-effort split over live model output, not a validation gate —
    grounding/correctness checking is a separate, not-yet-built concern
    (the JSON block is kept by the caller specifically so a later
    process can reuse grounding_gate.py's own logic against it if
    that's wanted).

    Three real cases, in the order a caller should reason about them:

    1. Fenced JSON block present, parses, non-empty text follows it —
       the normal, trained-for case. Returned as-is.
    2. No fenced JSON block at all — NOT an error. This is what a
       plain-text reply looks like (today's pre-fine-tune baseline
       behavior, and also what whatsapp_out_of_domain_directive's decline
       path will keep producing indefinitely, since that directive was
       never part of this fine-tuning dataset — the JSON-then-phrasing
       shape is Mode A's grounded-reply directive only). The full raw
       text is returned as the phrasing so nothing is lost.
    3. A fenced block is present but doesn't parse, OR parses but
       nothing usable follows it (the exact truncation shape
       scripts/finetune_data/teacher.py's TeacherCallResult.truncated()
       was built to catch during data generation — a syntactically
       complete JSON block whose trailing phrasing got cut off by
       max_tokens). Production has no stop_reason to check the way the
       offline teacher pipeline does, so this is detected structurally
       instead: if there's real text sitting after the fence, salvage it
       as the phrasing even though the JSON itself didn't parse; if
       there's genuinely nothing usable, the caller falls back to
       GENERATION_UNAVAILABLE_FALLBACK rather than showing a patient an
       empty or JSON-fragment reply."""
    match = _JSON_BLOCK_RE.search(raw_output)
    if not match:
        return None, raw_output.strip()

    phrasing = raw_output[match.end():].strip()

    try:
        parsed = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None, phrasing

    return parsed, phrasing


# U+0640 ARABIC TATWEEL — a pure calligraphic justification character
# (no semantic meaning, never a real letter or a word-separating space)
# that the source Excel's own header styling inserts between individual
# letters of field labels, e.g. "اســـم الـفـحـص" (confirmed by direct
# codepoint inspection of rag_execution.log's real CONTEXT dumps: every
# instance is literally U+0645 U+0640 U+0640 U+0633..., not repeated
# plain spaces). Preserved verbatim by ChunkingController per claude.md's
# real-source-only rule (§3.8) — stripping it here, at prompt-build time
# only, changes nothing ChunkingController stored or how it was derived;
# it only removes a decorative artifact that likely fragments these
# labels into far more subword tokens than the same label unstretched,
# for a model whose fine-tuning corpus is unlikely to have seen this
# exact stretched-Arabic-header formatting. Never touches U+0020 (a real
# word-separating space), so no two real words can ever be merged by
# this pass.
_TATWEEL_RE = re.compile("ـ+")


def _normalize_context_text(content: str) -> str:
    """Prompt-build-time-only readability pass over a retrieved chunk's
    raw content (2026-08-28, golden-suite v2 model-extraction-failure
    audit) — never touches what's stored, only what the LLM is shown.
    Two changes, both content-preserving (no character of real business
    text is added, removed, or reordered — only whitespace changes):

    1. Strips ARABIC TATWEEL (see _TATWEEL_RE above).
    2. Breaks each ". " (period-then-space) onto its own line. This is a
       deliberately conservative substitute for "one line per label:value
       field" — ChunkingController joins fields with exactly this ". "
       separator (see ChunkingController._concatenate_fields), but by the
       time content reaches here it's already flattened to one string
       with no field-boundary markers preserved, and many field VALUES
       themselves contain multiple ". "-separated sentences (real
       production examples: multi-sentence "تعليمات الحجز"/"ملاحظات"
       fields). Re-parsing the flat string to guess which ". " is a real
       field boundary vs. an ordinary in-value sentence break isn't
       reliable enough to trust blindly, so this doesn't try — it breaks
       on every ". " uniformly. That still turns one dense wall-of-text
       paragraph into a scannable list of lines (the actual goal — real
       evidence was CONTEXT blocks running 6000-8000 chars as one
       unbroken paragraph per source), it just doesn't guarantee every
       line is exactly one field. Never alters, drops, or merges a single
       character of real content — only some U+0020 spaces after periods
       become U+000A newlines."""
    text = _TATWEEL_RE.sub("", content)
    return text.replace(". ", ".\n")


def _is_json_empty(json_block) -> bool:
    """True when json_block extracted nothing at all — an empty dict
    (narrow shape) or a broad-shape list where every source's own fields
    dict is empty. Drives the widening retry in _mode_a_reply: a
    completely empty extraction is the model's own signal that the
    narrow CONTEXT it was given didn't answer the question, worth trying
    again with the wider broad-ceiling candidate set already fetched for
    this same turn. None (no JSON block at all — the out-of-domain
    decline path, or a not-yet-fine-tuned model's plain-text output) is
    deliberately NOT "empty" here: that's a structurally different shape
    the retry isn't meant to touch."""
    if json_block is None:
        return False
    if isinstance(json_block, dict):
        return not json_block
    if isinstance(json_block, list):
        return all(not (isinstance(entry, dict) and entry.get("fields")) for entry in json_block)
    return False


class TextReplyController(BaseController):
    """Phase 1's dual-mode reply logic (Implementation Plan Step 1).
    Mode A: grounded, retrieval-scaled rewrite — single-chunk, unwrapped
    for narrow queries (see _narrow_context_block), multi-chunk wrapped
    synthesis for broad ones, always in Egyptian Arabic, always closing
    with a dynamic follow-up question. Mode B:
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

    Full-chunk narrow CONTEXT (2026-08-25, superseding the original
    FieldSelectionController design): narrow-breadth CONTEXT is each
    retrieved chunk's full, real content, unfiltered — see
    _narrow_context_block. The earlier design pre-filtered this down to
    the 1-2 fields an embedding-similarity match said were relevant
    before the model ever saw the chunk; real evidence (this session's
    own root-cause work on the cross-brand-referral regression) showed
    that similarity ranking reliably lost to a field whose label read
    nothing like the question but whose value was exactly the answer,
    with no query-independent fix available. The fine-tuned model is now
    trained to make that field-selection judgment itself, directly from
    the full chunk (scripts/finetune_data/sampling.py Pass 1) — this
    controller's job narrowed to matching that same input shape exactly,
    not to pre-selecting content on the model's behalf.

    JSON-then-phrasing output: the Section 3 Step 1 fine-tuning dataset
    (scripts/finetune_data_out/) trains the model to reason in a fenced
    ```json block first, then phrase the patient-facing reply from only
    what that JSON contains — the same anti-hallucination discipline
    Stage 5's grounding gate enforces on the training data itself, now
    asked of the live model at inference time. _mode_a_reply splits the
    two apart (_split_json_and_phrasing) and returns ONLY the phrasing —
    the JSON never reaches the patient, it's logged for internal
    visibility only. This is deliberately tolerant, not a strict
    contract: a reply with no JSON block at all (today's pre-fine-tune
    baseline, and whatsapp_out_of_domain_directive's decline path, which
    this dataset never covers) is treated as ordinary plain-text output,
    not an error — see the helper's own docstring for the full case
    breakdown.

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
        reply_verification_controller,
    ):
        super().__init__()
        self.retrieval_controller = retrieval_controller
        self.client_config_model = client_config_model
        self.generation_client = generation_client
        self.dialogue_state_template_map_model = dialogue_state_template_map_model
        self.reply_verification_controller = reply_verification_controller
        self.template_parser = TemplateParser()
        self.logger = logging.getLogger(__name__)

    async def reply(self, client_id: str, session_id, text: str, session_state: dict) -> tuple[str, str, object | None]:
        """Returns (reply_text, mode, debug_json) — mode is "mode_a" or
        "mode_b", surfaced for logging/Postman verification, never used
        by the caller to branch (the decision already happened here).

        debug_json is the fine-tuned model's own extracted JSON block
        (see _split_json_and_phrasing) for Mode A turns that produced
        one, always None otherwise — Mode B is verbatim template
        substitution with no model call at all, and Mode A's zero-chunk
        out-of-domain decline was never part of this fine-tuning dataset
        so it stays plain text. Never shown to the patient (routes/
        schemes/whatsapp.py surfaces it as its own ChatResponse.debug_json
        field, entirely separate from `reply`) — it exists purely for
        internal tooling (scripts/collect_golden_responses.py's grounding
        check) to compare the model's own extracted facts against a golden
        case's expected_fact without having to re-parse `reply` itself."""
        dialogue_state = session_state.get("dialogue_state")
        if dialogue_state:
            mapping = await self.dialogue_state_template_map_model.get_template_for_state(client_id, dialogue_state)
            if mapping is not None:
                bucket = TemplateBucket.B if mapping.bucket == "B" else TemplateBucket.C
                substitutions = self._mode_b_substitutions(session_state)
                return self.template_parser.resolve(bucket, mapping.template_id, **substitutions), "mode_b", None

        reply_text, json_block = await self._mode_a_reply(client_id, session_id, text, session_state)
        return reply_text, "mode_a", json_block

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

    async def _narrow_context_block(self, results: list[dict]) -> str:
        """Builds narrow-breadth CONTEXT from each already-retrieved
        chunk's full, real content — unfiltered. Matches exactly what the
        fine-tuned model is trained on (scripts/finetune_data/sampling.py
        Pass 1, 2026-08-25 redesign): the model itself now decides which
        field(s) in the full chunk answer the question, replacing the old
        FieldSelectionController pre-filtering step (deleted — this was
        its only caller).

        Deliberately unwrapped, no [BEGIN SOURCE n] boundary — narrow is
        exactly one chunk (whatsapp_retrieval_top_k_narrow == 1), the
        same single-chunk shape the fine-tuned model actually trained on.

        2026-08-26 history: top_k_narrow was briefly raised to 3, with
        this method wrapping multi-chunk narrow CONTEXT the same way
        broad mode does. Reverted after a real-data retrieval
        investigation (scripts/investigate_retrieval_task1.py) on the
        resulting regressions: cases with an overwhelmingly dominant,
        unambiguous single correct chunk (score 7.08, repeated in every
        retrieved candidate) still failed to extract once wrapped as
        multiple sources — retrieval clearly wasn't the bottleneck, so
        this had to be the untrained wrapped/multi-chunk format itself.
        One case also showed a concrete, confirmed distractor: a
        topically-adjacent-but-wrong row (different company, different
        number, same field label) that only entered the window because
        top_k_narrow exceeded 1. Widening (Solution 2's retry, still
        below) now carries the entire "give the model more chunks"
        responsibility instead — it only escalates to the wrapped,
        multi-source broad-ceiling format when the single unwrapped
        chunk's own extraction genuinely comes back empty, never
        unconditionally.

        2026-08-28: each chunk's content is passed through
        _normalize_context_text first — see that helper's own docstring.
        Presentation-layer only, same real content, never touches what
        ChunkingController stored."""
        return "\n\n".join(_normalize_context_text(result["chunk"].content) for result in results)

    @staticmethod
    def _wrap_sources(results: list[dict]) -> str:
        """Wraps each retrieved chunk's full content with an explicit
        [BEGIN SOURCE n]/[END SOURCE n] boundary. Real testing showed the
        model misattributing a qualifier from one chunk to an entity
        named in a different one when chunks were separated only by a
        bullet "-" and each chunk's own embedded "[Document: ...]"
        prefix. Used by broad-breadth CONTEXT and by the widening retry's
        wider context (_mode_a_reply) — never by narrow breadth
        (_narrow_context_block), which stays single-chunk and unwrapped
        on purpose (see that method's own docstring for why).

        2026-08-28: each chunk's content is passed through
        _normalize_context_text first — see that helper's own docstring."""
        return "\n\n".join(
            f"[BEGIN SOURCE {index}]\n{_normalize_context_text(result['chunk'].content)}\n[END SOURCE {index}]"
            for index, result in enumerate(results, start=1)
        )

    async def _generate_grounded_reply(
        self, text: str, history: list, system_prompt: str, context_block: str,
        cross_brand_note: str | None, max_tokens: int,
    ) -> tuple[object | None, str, str]:
        """One real generation call against a given context_block,
        returning (json_block, phrasing, raw_output) via
        _split_json_and_phrasing — raw_output is passed through
        unmodified alongside the split, so a caller that needs it (the
        no-usable-phrasing warning log below) doesn't have to re-derive
        it from json_block/phrasing. Factored out of _mode_a_reply so the
        widening retry (breadth == narrow, first attempt's JSON came back
        completely empty — see _is_json_empty) can re-run this exact same
        sequence against a wider context_block without duplicating
        message-building or JSON-split logic. Raises GenerationTimeoutError
        rather than catching it — the two call sites in _mode_a_reply need
        different behavior on timeout (the first attempt falls back to
        GENERATION_UNAVAILABLE_FALLBACK; a timed-out retry just keeps
        whatever the first attempt already produced), so the decision
        belongs to the caller, not this helper."""
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
        raw_output = await self.generation_client.generate_reply(messages, temperature=0.0, max_tokens=max_tokens)
        json_block, phrasing = _split_json_and_phrasing(raw_output)
        return json_block, phrasing, raw_output

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

    async def _mode_a_reply(
        self, client_id: str, session_id, text: str, session_state: dict,
    ) -> tuple[str, object | None]:
        """Returns (phrasing, json_block) — see reply()'s own docstring
        for what json_block is and who consumes it. json_block is always
        None on the two fallback paths below (a timeout or an unusable
        generation has no JSON to report) and on the out-of-domain decline
        path (never part of this fine-tuning dataset).

        The final step, before returning, is ReplyVerificationController's
        safety gate (Implementation Plan's post-fine-tuning Step 8): the
        phrasing this method is about to return gets checked against its
        own json_block for unverified numeric claims, and swapped for a
        safe fallback (with the real draft escalated to human_handoff_queue)
        if it fails. This runs on every real Mode A answer — never on the
        out-of-domain decline or the two earlier fallback returns below,
        which either have no json_block to verify against or are already
        the safe fallback themselves.

        Widening retry (2026-08-26, golden-suite v2 FP-handoff audit): if
        breadth is narrow and the first generation's own JSON extraction
        comes back completely empty (_is_json_empty) — the model's own
        signal that its narrow slice didn't answer the question — this
        retries generation ONCE against the wider broad-ceiling candidate
        set already fetched for this same turn (no second retrieval call
        needed; see all_results below). This never asserts an answer
        exists — it only gives the model more real evidence and lets it
        decide again, so it can still decline on the retry. The result,
        retried or not, still goes through ReplyVerificationController
        unchanged."""
        history = session_state.get("history", [])
        client_config = await self.client_config_model.get_client_config(client_id)

        # "brand" — not "Account" — matches ChunkingController's real,
        # promoted top-level metadata key (value-matched against
        # client_config.brand_value_aliases at chunk time, canonical
        # lowercase). "Account" was never a real, filterable metadata
        # key — it only ever existed nested inside field_data, under 4
        # different spellings depending on the sheet.
        brand_filter = session_state.get("brand_filter")
        metadata_filters = None
        if brand_filter and "brand" in (client_config.allowed_metadata_keys or []):
            metadata_filters = {"brand": brand_filter}

        # Retrieve at the broad ceiling unconditionally, first — breadth
        # is now decided FROM these results' own score distribution, not
        # before retrieval runs (see _classify_breadth). This replaces a
        # second classify_intent(["narrow","broad"]) LLM call that real
        # traffic proved unreliable: literal [BEGIN SOURCE] scaffolding
        # was leaking into patient-facing replies for questions that were
        # unambiguously narrow, because that wrapper only exists on the
        # broad-path branch below — proof the old classifier was routing
        # narrow questions there, which also meant FieldSelectionController
        # never ran on those turns at all. Kept as all_results (never
        # overwritten) so the widening retry below has the full broad-
        # ceiling candidate set on hand without a second retrieval call.
        broad_top_k = client_config.whatsapp_retrieval_top_k_broad
        all_results = await self.retrieval_controller.retrieve(
            client_id=client_id, query=text, metadata_filters=metadata_filters, top_k_override=broad_top_k,
        )

        cross_brand_note = None
        if not all_results and metadata_filters:
            # Cross-brand suggestion: before declining, check whether the
            # service exists under the sibling brand and say so plainly,
            # grounded in the real, already-transcribed cross-referral
            # directive rather than an invented instruction.
            unfiltered_results = await self.retrieval_controller.retrieve(
                client_id=client_id, query=text, metadata_filters=None, top_k_override=broad_top_k,
            )
            if unfiltered_results:
                all_results = unfiltered_results
                cross_brand_note = self.template_parser.resolve(
                    TemplateBucket.C, "directive_brand_cross_referral",
                )

        breadth = self._classify_breadth(text, all_results, client_config)
        # Slice down to the narrow ceiling only now that breadth is
        # decided — never re-retrieved, just the same real all_results
        # trimmed, so this can't disagree with what was just scored.
        results = all_results[:client_config.whatsapp_retrieval_top_k_narrow] if breadth == "narrow" else all_results

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
            # temperature/decoding history for every generate_reply call
            # in this method: see the widening-retry block below for the
            # full 0.1 -> 0.2 -> 0.15 -> 0.0 story; this out-of-domain
            # call uses the same final value (0.0) for the same reasons.
            try:
                raw_output = await self.generation_client.generate_reply(messages, temperature=0.0, max_tokens=60)
            except GenerationTimeoutError as e:
                # Root-caused against real Falcon-H1 golden-suite traffic
                # (Phase 0 bake-off): a slow/eager-mode remote deployment
                # can legitimately exceed the request timeout. This is one
                # of two call sites allowed to return non-Bucket-B,
                # non-model text — see GENERATION_UNAVAILABLE_FALLBACK's
                # own comment for why.
                self.logger.warning(f"Mode A: generation timed out for {text!r} ({e}) — returning fallback reply")
                return GENERATION_UNAVAILABLE_FALLBACK, None
            json_block, phrasing = _split_json_and_phrasing(raw_output)
        else:
            system_prompt = self.template_parser.resolve(TemplateBucket.C, "whatsapp_mode_a_reply_directive")

            if breadth == "narrow":
                context_block = await self._narrow_context_block(results)
            else:
                # Broad queries keep full multi-chunk content — Rule 5
                # explicitly wants breadth here (a summary across several
                # services), so narrowing down to "the most relevant
                # field" per chunk would work against the task, not for
                # it. Each chunk gets explicit structural delimiters via
                # _wrap_sources — real testing showed the model
                # misattributing a qualifier from one chunk to an entity
                # named in a different one during broad-query synthesis,
                # when chunks were separated only by a bullet "-" and each
                # chunk's own embedded "[Document: ...]" prefix.
                context_block = self._wrap_sources(results)

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

            # Breadth-aware budget — raised from 250/100 after the
            # post-fine-tune golden-suite grading (68.7% run) found ~11/67
            # replies truncated mid-sentence, including 4 where the cutoff
            # landed inside the JSON reasoning block itself and leaked raw,
            # unparseable JSON to the patient. The old budgets were sized
            # around the *training* target-length distribution (max ~230
            # tokens for the longest broad_positive examples), but real
            # production CONTEXT/debug_json shapes run longer than that
            # training sample — narrow turns got cut too (e.g. a single
            # branch-name field mid-word), not just broad ones. 1024/2048
            # give real headroom past every truncation case observed in
            # that grading pass; the model still stops naturally at
            # NILE_CHAT_STOP_SEQUENCES well before either ceiling on a
            # normal-length reply, so this is a ceiling raise, not a
            # verbosity change.
            max_tokens = 2048 if breadth == "broad" else 1024

            # temperature history on this call: 0.1 (too greedy — got
            # stuck in loops when combined with repetition_penalty=1.1,
            # since reverted), then 0.2 (fixed that, but loosened things
            # enough that the model started hallucinating numbers/
            # timestamps instead of copying them from CONTEXT), then 0.15
            # as the split-the-difference value. Lowered to 0.0 (greedy
            # decoding) after repeated golden-suite runs at 0.15 kept
            # landing on the same ~80.6% accuracy with a DIFFERENT failure
            # mix each time (over-caution vs. hallucination counts
            # shuffling run to run, same overall total) — real evidence
            # that a meaningful share of the remaining gap was sampling
            # noise, not a stable, fixable pattern. 0.0 removes that noise
            # from evaluation so a future prompt/code change's real effect
            # is visible in one run instead of needing several averaged
            # together. Real, disclosed risk carried over from the 0.1
            # history above: greedy decoding is generally MORE prone to
            # repetition loops than mild sampling, since there's no random
            # escape once the model locks onto a repeating path — watch
            # new golden-suite output for that exact symptom (a phrase or
            # clause repeated verbatim within one reply) before trusting
            # this as a durable production value, not just an eval-time
            # one.
            try:
                json_block, phrasing, raw_output = await self._generate_grounded_reply(
                    text, history, system_prompt, context_block, cross_brand_note, max_tokens,
                )
            except GenerationTimeoutError as e:
                self.logger.warning(f"Mode A: generation timed out for {text!r} ({e}) — returning fallback reply")
                return GENERATION_UNAVAILABLE_FALLBACK, None

            # Widening retry: the narrow slice's own JSON extraction came
            # back completely empty — the model's own signal that the
            # single unwrapped chunk it saw didn't answer the question.
            # Retry once against the wider broad-ceiling candidate set
            # already sitting in all_results (no second retrieval call).
            # This is now the ONLY mechanism that ever shows the model
            # more than one chunk on a narrow turn (whatsapp_retrieval_
            # top_k_narrow reverted to 1, 2026-08-26 — see
            # _narrow_context_block's docstring for why unconditionally
            # widening narrow's own top_k caused real regressions instead).
            # Never fires when breadth was already broad (that's already
            # the widest this turn's retrieval offers — there's no more
            # real evidence to give it) or when narrow already saw every
            # available result (len(all_results) == len(results), e.g.
            # all_results itself had exactly 1 chunk).
            if breadth == "narrow" and _is_json_empty(json_block) and len(all_results) > len(results):
                self.logger.info(
                    f"[widening_retry] narrow extraction empty for {text!r}, retrying with broad "
                    f"context ({len(all_results)} sources)"
                )
                try:
                    wider_context_block = self._wrap_sources(all_results)
                    self.logger.debug(
                        f"[widening_retry] query={text!r} full_context_block=\n{wider_context_block}"
                    )
                    json_block, phrasing, raw_output = await self._generate_grounded_reply(
                        text, history, system_prompt, wider_context_block, cross_brand_note, max_tokens,
                    )
                except GenerationTimeoutError as e:
                    # Best-effort only — if the retry itself times out,
                    # silently keep the original (empty-JSON) narrow
                    # attempt's result rather than failing the whole turn;
                    # it still flows into the exact same downstream
                    # empty-phrasing-fallback / ReplyVerificationController
                    # path it would have without this retry existing.
                    self.logger.warning(
                        f"[widening_retry] retry generation timed out for {text!r} ({e}) — keeping original attempt"
                    )

        if json_block is not None:
            # DEBUG, matching this method's existing convention for real
            # patient-adjacent business content (see the [context] logs
            # above) — opt-in verbosity, not logged by default.
            self.logger.debug(f"[mode_a_json] query={text!r} json_block={json_block!r}")
        else:
            # INFO, not a warning: a missing JSON block is the expected,
            # unremarkable shape for whatsapp_out_of_domain_directive's
            # decline path (never part of this fine-tuning dataset) and
            # for any turn still served by a not-yet-fine-tuned model —
            # a real signal worth having on hand for rollout monitoring,
            # but not itself evidence of a problem.
            self.logger.info(f"[mode_a_json] query={text!r} no JSON block found — treating full output as phrasing")

        if not phrasing:
            # A JSON block was found and parsed, but nothing usable
            # followed it — the live-inference shape of the truncation
            # failure scripts/finetune_data/teacher.py's
            # TeacherCallResult.truncated() was built to catch during
            # data generation. Never show a patient an empty reply or a
            # bare JSON fragment; fall back exactly like a generation
            # timeout does.
            self.logger.warning(
                f"Mode A: generation for {text!r} produced no usable phrasing after JSON "
                f"extraction (raw_output={raw_output!r}) — returning fallback reply"
            )
            # json_block itself may still be a validly-parsed dict/list
            # here (only the phrasing half was unusable) — deliberately
            # NOT surfaced as debug_json in this branch: the patient is
            # getting GENERATION_UNAVAILABLE_FALLBACK, not the model's
            # real answer, so pairing that fallback text with a real
            # extracted JSON block would misrepresent what was actually
            # shown for this turn.
            return GENERATION_UNAVAILABLE_FALLBACK, None

        # Unlike the truncation-fallback branch just above, json_block here
        # is a real, successfully-extracted artifact of what the model
        # actually computed — it's the reason a rejection would even be
        # detectable. So it's still returned as debug_json even if the
        # gate below swaps out the phrasing: a human reviewing
        # human_handoff_queue (or scripts/collect_golden_responses.py)
        # needs exactly this JSON to see what the model got right in
        # extraction but phrased ungrounded, which is a different, more
        # specific failure than "no real answer was produced at all".
        verified_reply = await self.reply_verification_controller.verify_and_gate(
            client_id, session_id, text, phrasing, json_block,
        )
        return verified_reply, json_block
