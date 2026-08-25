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


def _check_phrasing_numeric_grounding(phrasing: str, allowed_values_text: str) -> GateResult:
    """Identical algorithm to grounding_gate.py's own
    _check_phrasing_numeric_grounding: every number in a claim sentence
    (one not ending in ؟/?) must appear among the allowed values' own
    numbers. Hard auto-reject only — see that module's docstring for the
    disclosed scope limit (faithfulness, not relevance; numeric claims
    only, not every kind of claim drift)."""
    allowed_numbers = set(NUMBER_TOKEN_RE.findall(allowed_values_text))
    for sentence in re.split(r"(?<=[.!؟?])\s+", phrasing):
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
    ) -> str:
        """Returns the reply text that's actually safe to show the
        patient: `phrasing` unchanged if it passes (or if there's no
        json_block to check it against), REPLY_VERIFICATION_FAILED_FALLBACK
        if it's rejected. Never raises — a bug in this gate must never
        take down a turn that would otherwise have gone out clean."""
        if json_block is None:
            return phrasing

        result = _check_phrasing_numeric_grounding(phrasing, _json_values_text(json_block))
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
