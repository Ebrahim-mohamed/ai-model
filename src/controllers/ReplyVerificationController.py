import logging
import re

from .BaseController import BaseController

# Same token shape as scripts/finetune_data/grounding_gate.py's
# NUMBER_TOKEN_RE (deliberately not imported from there — scripts/ is
# never imported by src/ at runtime, claude.md's directory tree marks it
# "one-shot admin CLIs... never imported by src/"). This is the live,
# production-side sibling of that training-time gate: same regex, same
# sentence-splitting/question-exclusion rule, re-implemented here because
# the two run against structurally different inputs (a raw training
# record's `grounding` block there, vs. a live Mode A turn's own
# extracted `debug_json` here) even though the underlying check is
# identical in spirit.
NUMBER_TOKEN_RE = re.compile(r"\d+(?:[:.,]\d+)*")

# Deliberately NOT a Bucket B template — same reasoning as
# TextReplyController.GENERATION_UNAVAILABLE_FALLBACK: this describes no
# business fact, policy, or script, only a technical safety-gate outcome,
# so it doesn't belong in prompt_templates.py's real-source-only content
# (claude.md §3.5). Kept here, narrowly scoped to this one call site.
REPLY_VERIFICATION_FAILED_FALLBACK = (
    "معذرة، محتاجة أتأكد من المعلومة دي مع فريقنا الأول عشان أضمن دقتها. "
    "هوصلك حد من الفريق يرد عليك بأسرع وقت."
)


class GateResult:
    """Same small shape as grounding_gate.py's GateResult, kept local to
    this file rather than shared — see the module docstring above for why
    src/ doesn't import scripts/."""

    def __init__(self, accepted: bool, reason: str = "", detail: dict | None = None):
        self.accepted = accepted
        self.reason = reason
        self.detail = detail or {}

    @classmethod
    def accept(cls) -> "GateResult":
        return cls(True)

    @classmethod
    def reject(cls, reason: str, **detail) -> "GateResult":
        return cls(False, reason=reason, detail=detail)


def _json_values_text(json_block) -> str:
    """Flattens a Mode A turn's extracted debug_json into the same
    "allowed values" text grounding_gate.py builds from output_json/fields
    at training time. Two real shapes reach here (see
    TextReplyController._split_json_and_phrasing / whatsapp_mode_a_reply_directive):
    a flat dict for a narrow-breadth answer, or a list of
    {"source": n, "fields": {...}} entries for a broad-breadth one. Any
    other shape (or an empty container) yields "" — an empty allowed-set,
    which correctly rejects any phrasing that still cites a number."""
    if isinstance(json_block, dict):
        return " ".join(str(v) for v in json_block.values())
    if isinstance(json_block, list):
        parts = []
        for entry in json_block:
            if isinstance(entry, dict):
                fields = entry.get("fields", {})
                if isinstance(fields, dict):
                    parts.extend(str(v) for v in fields.values())
        return " ".join(parts)
    return ""


def _check_phrasing_numeric_grounding(phrasing: str, allowed_values_text: str, patient_message: str = "") -> GateResult:
    """Same algorithm as grounding_gate.py's own
    _check_phrasing_numeric_grounding, with two live-production-only
    refinements (2026-08-25 golden-suite v2 audit): every number in a
    claim sentence (one not ending in ؟/?) must appear among the allowed
    values' own numbers, OR among the numbers the patient themselves
    typed in patient_message — a number the patient already stated (e.g.
    echoing their own age back while explaining an eligibility rule) is
    definitionally not a model fabrication, so it's folded into the
    allowed set rather than treated as an unverified claim. Sentences are
    now split per-line first, THEN by sentence-ending punctuation within
    each line: the previous whole-phrasing split let a multi-line
    numbered list with no internal '.'/'!'/'؟' get treated as one giant
    "sentence" that only counted as a question if the very last line
    happened to end in '؟' — silently skipping every number inside the
    list from grounding-checking entirely. Hard auto-reject only — see
    grounding_gate.py's own docstring for the disclosed scope limit
    (faithfulness, not relevance; numeric claims only, not every kind of
    claim drift).

    A leading list/ordinal marker ("1- ", "2. ", "3) ") is stripped from
    each line before the number search — caught during verification of
    this exact fix: a genuinely fully-grounded reply that happens to
    phrase itself as a numbered list (the model's own formatting choice,
    not necessarily inherited from the source text's own shape) would
    otherwise have its "1"/"2"/"3" markers themselves flagged as
    unverified numeric claims, a false rejection unrelated to whether the
    real facts in the list are grounded. Only a marker at the START of a
    line is stripped (digit immediately followed by -/./) then
    whitespace) — a real fact number never has that exact shape, so this
    can't hide a genuine unverified number.

    The clause-split now also breaks on a comma (Arabic '،' or ASCII ',')
    followed by whitespace, not just '.'/'!'/'؟'/'?' — caught during the
    2026-08-25 post-fix golden-suite re-run: a single-line reply that
    joins a real factual claim to its trailing follow-up question with a
    comma instead of a period ("...بدلا من 1705 يا فندم، تحب أحجزلك
    فيها؟") was still being swallowed whole as "one sentence ending in a
    question mark", exempting the real numbers in the factual half from
    grounding entirely — the same root problem the line-split fix above
    closed for multi-line lists, in a single-line shape that fix didn't
    reach. Splitting only counts a comma as a boundary when it's followed
    by whitespace, deliberately: a thousands-separator comma inside a
    number ("1,000") is never followed by whitespace, so it can't be
    mistaken for a clause boundary and split apart into two number
    tokens."""
    allowed_numbers = (
        set(NUMBER_TOKEN_RE.findall(allowed_values_text))
        | set(NUMBER_TOKEN_RE.findall(patient_message))
    )
    list_marker_re = re.compile(r"^\s*\d+[-.\)]\s+")
    for line in phrasing.splitlines():
        line = list_marker_re.sub("", line)
        for sentence in re.split(r"(?<=[.!؟?,،])\s+", line):
            stripped = sentence.strip()
            if not stripped or stripped.endswith(("؟", "?")):
                continue
            for number in NUMBER_TOKEN_RE.findall(stripped):
                if number not in allowed_numbers:
                    return GateResult.reject("unverified_numeric_claim", sentence=stripped, number=number)
    return GateResult.accept()


class ReplyVerificationController(BaseController):
    """Section 3 Step 1's post-fine-tuning safety gate (Implementation
    Plan's Step 8, following Step 7's LoRA fine-tune). Sits between
    TextReplyController._mode_a_reply's raw generation and the reply
    actually shown to the patient: re-runs the same deterministic
    numeric-grounding check the training-data QA gate
    (scripts/finetune_data/grounding_gate.py) already proved has zero
    false positives standalone, now against the live model's own
    extracted debug_json instead of a training record's structured
    source. A live turn with no JSON block at all (out-of-domain decline,
    or a not-yet-fine-tuned model's plain-text output) has nothing to
    verify against and passes through unchanged — same tolerant treatment
    _split_json_and_phrasing already gives that shape.

    On rejection, the real draft reply is withheld from the patient (never
    shown, never silently softened) and a row is written to
    human_handoff_queue so a human agent can review and answer the turn
    directly. This is the one call site allowed to write that table."""

    def __init__(self, human_handoff_queue_model):
        super().__init__()
        self.human_handoff_queue_model = human_handoff_queue_model
        self.logger = logging.getLogger(__name__)

    async def verify_and_gate(
        self,
        client_id: str,
        session_id,
        patient_message: str,
        phrasing: str,
        json_block,
        context_block: str | None = None,
    ) -> str:
        """Returns the reply text that's actually safe to show the
        patient: `phrasing` unchanged if it passes (or if there's no
        json_block to check it against), REPLY_VERIFICATION_FAILED_FALLBACK
        if it's rejected. Never raises — a bug in this gate must never
        take down a turn that would otherwise have gone out clean.

        `context_block` (2026-09-02 audit finding, optional/backward-
        compatible — defaults to None) is the raw CONTEXT text
        TextReplyController actually showed the model for this turn's
        final generation attempt. Folded into the allowed-numbers pool
        alongside json_block's own extracted values: real production
        evidence showed the model's own JSON field-selection step is
        sometimes incomplete — either every source's `fields` comes back
        empty on a broad turn while the phrasing still correctly quotes a
        real CONTEXT fact (reproduced twice on identical real traffic:
        'رنين علي المخ' Creatinine/14-day prep fact), or a specific field
        is simply never selected even though its value appears verbatim
        in the phrasing (a CBCT exam's own name, 'CBCT (3D) Single Arch',
        never captured as a field — flagging the literal '3' inside the
        exam's own product name as an "unverified numeric claim"). Any
        number genuinely present in the real CONTEXT the model was shown
        is by definition not a fabrication, regardless of whether the
        model's own JSON bookkeeping happened to capture it — this
        doesn't loosen what counts as a real hallucination (a number that
        appears nowhere in the real evidence shown this turn), it only
        fixes the pool of "real evidence" this check was measuring
        against, which was previously narrower than what the model
        actually saw. `context_block` is None on the zero-chunk
        out-of-domain decline path — harmless, since that path also never
        produces a json_block, and this method already returns early
        on `json_block is None` before context_block is ever touched."""
        if json_block is None:
            return phrasing

        allowed_values_text = _json_values_text(json_block)
        if context_block:
            allowed_values_text = f"{allowed_values_text} {context_block}"

        result = _check_phrasing_numeric_grounding(phrasing, allowed_values_text, patient_message)
        if result.accepted:
            return phrasing

        self.logger.warning(
            f"[reply_verification] REJECTED client_id={client_id} session_id={session_id} "
            f"reason={result.reason!r} detail={result.detail} phrasing={phrasing!r}"
        )
        await self.human_handoff_queue_model.enqueue(
            client_id=client_id,
            session_id=session_id,
            patient_message=patient_message,
            draft_reply=phrasing,
            rejection_reason=result.reason,
            rejection_detail=result.detail,
        )
        return REPLY_VERIFICATION_FAILED_FALLBACK
