import logging

from .BaseController import BaseController
from stores.llm.templates.template_parser import TemplateParser, TemplateBucket


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
    tuning issue. No hardcoded customer-facing fallback strings live here
    either — every reply this controller returns is either a resolved
    Bucket B/C template or live model output.

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
    ):
        super().__init__()
        self.retrieval_controller = retrieval_controller
        self.client_config_model = client_config_model
        self.generation_client = generation_client
        self.dialogue_state_template_map_model = dialogue_state_template_map_model
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

    async def _mode_a_reply(self, client_id: str, text: str, session_state: dict) -> str:
        # Query-breadth classification reuses the same closed-set
        # classification capability intent-routing already uses — not a
        # second model, just a second call with a different label set.
        # History passed here too (same reasoning as intent routing): a
        # short follow-up's breadth is only resolvable with context.
        history = session_state.get("history", [])
        breadth_guidance = self.template_parser.resolve(TemplateBucket.C, "whatsapp_breadth_classification_guidance")
        breadth = await self.generation_client.classify_intent(
            text, ["narrow", "broad"], history=history, guidance=breadth_guidance,
        )

        client_config = await self.client_config_model.get_client_config(client_id)
        top_k = (
            client_config.whatsapp_retrieval_top_k_broad
            if breadth == "broad"
            else client_config.whatsapp_retrieval_top_k_narrow
        )

        brand_filter = session_state.get("brand_filter")
        metadata_filters = None
        if brand_filter and "Account" in (client_config.allowed_metadata_keys or []):
            metadata_filters = {"Account": brand_filter}

        results = await self.retrieval_controller.retrieve(
            client_id=client_id, query=text, metadata_filters=metadata_filters, top_k_override=top_k,
        )

        cross_brand_note = None
        if not results and metadata_filters:
            # Cross-brand suggestion: before declining, check whether the
            # service exists under the sibling brand and say so plainly,
            # grounded in the real, already-transcribed cross-referral
            # directive rather than an invented instruction.
            unfiltered_results = await self.retrieval_controller.retrieve(
                client_id=client_id, query=text, metadata_filters=None, top_k_override=top_k,
            )
            if unfiltered_results:
                results = unfiltered_results
                cross_brand_note = self.template_parser.resolve(
                    TemplateBucket.C, "directive_brand_cross_referral",
                )

        # Relevance gate — narrow queries only. Real calibration against
        # the live reranker (cross-encoder, raw unbounded logit score)
        # showed a clean, wide separation for narrow single-fact queries:
        # genuine in-domain top-1 scores of 5.13 and 0.86, vs. -1.3 to
        # -5.8 for out-of-domain/adjacent/nonsense queries (including the
        # real "بتعملوا زراعة أسنان؟" case that motivated this). Broad
        # queries do NOT get this check: a genuinely in-domain broad
        # query ("عندكم أشعة إيه؟") scored -1.24 in that same calibration
        # — indistinguishable from a genuinely out-of-domain one — because
        # no single chunk fully answers an open-ended question even when
        # several are legitimately relevant in aggregate. Gating those on
        # a flat score would false-decline real in-scope queries.
        if breadth == "narrow" and results and results[0]["score"] < client_config.whatsapp_min_relevance_score:
            self.logger.info(
                f"Mode A: top score {results[0]['score']:.4f} below relevance gate "
                f"({client_config.whatsapp_min_relevance_score}) for {text!r}, routing to out-of-domain"
            )
            results = []
            cross_brand_note = None

        if not results:
            # Zero retrieved chunks (or gated below the relevance floor):
            # the out-of-domain directive is deliberately given NO context
            # to draw from, so the model has nothing to fabricate a fact
            # from — it can only acknowledge the request and redirect.
            self.logger.info(f"Mode A: no usable chunks for {text!r}, generating dynamic out-of-domain decline")
            system_prompt = self.template_parser.resolve(TemplateBucket.C, "whatsapp_out_of_domain_directive")
            messages = [
                {"role": "system", "content": system_prompt},
                *history,
                {"role": "user", "content": f"PATIENT MESSAGE:\n{text}"},
            ]
            # Out-of-domain replies are always 1-2 sentences by directive,
            # regardless of the original query's breadth classification.
            max_tokens = 100
        else:
            system_prompt = self.template_parser.resolve(TemplateBucket.C, "whatsapp_mode_a_reply_directive")

            # Each chunk gets explicit structural delimiters — real testing
            # showed the model misattributing a qualifier from one chunk to
            # an entity named in a different one during broad-query
            # synthesis, when chunks were separated only by a bullet "-"
            # and each chunk's own embedded "[Document: ...]" prefix. A
            # numbered [BEGIN SOURCE n]/[END SOURCE n] wrapper gives the
            # model an explicit, unambiguous boundary to keep facts scoped
            # to their actual source, without hardcoding any content.
            context_block = "\n\n".join(
                f"[BEGIN SOURCE {index}]\n{result['chunk'].content}\n[END SOURCE {index}]"
                for index, result in enumerate(results, start=1)
            )

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

        # temperature=0.1 (near-greedy) was tried and reverted: combined
        # with this quantized model's known instability, it left the
        # model no room to escape a bad token trajectory once one
        # started, producing loops and degenerate output rather than the
        # intended "stay factual" effect. 0.2 keeps generation close to
        # deterministic (this is still a grounded extraction/rewrite
        # task, not a creative one) while giving the sampler enough
        # margin to route around a bad first token instead of committing
        # to it.
        return await self.generation_client.generate_reply(messages, temperature=0.2, max_tokens=max_tokens)
