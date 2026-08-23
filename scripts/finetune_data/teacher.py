"""Wrapper around the Anthropic API for teacher distillation — the shared
call primitive plus prompt-building and robust response-parsing utilities
used by every one of sampling.py's four passes. Mirrors
llm_finetuning.ipynb's own distillation shape (system+user messages,
schema described in-prompt, "```json" prefix trick to bias the output
format) — adapted per claude.md §6.4 because we have no single universal
Pydantic schema the way the notebook's NewsDetails was: the JSON keys
allowed for a given example are whatever real field_data labels that
example's sampled chunk(s) actually carry, so the schema hint is
instance-specific text, built fresh per call, not one constant.

Deliberately NOT wired through stores/llm/ or stores/generation/: those
are Ports & Adapters for the LIVE request-serving app (claude.md §1.2),
and this is an offline, one-shot scripts/ tool that never touches live
traffic. Retrofitting Claude into either interface would either be a bad
abstraction fit (stores/generation/ is shaped around an OpenAI-compatible
vLLM endpoint) or force runtime-swappability machinery this script has no
use for — over-engineering claude.md §1.2's own guidance explicitly warns
against.
"""

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass, field

import anthropic

DEFAULT_MODEL = "claude-opus-5"
DEFAULT_MAX_TOKENS = 1000
# Broad-positive's JSON block alone (up to 5 per-source entries, each
# with several fields) can eat most of DEFAULT_MAX_TOKENS before the
# phrasing even starts — confirmed for real by this pipeline's own
# --limit smoke test, which produced exactly one silently truncated
# phrasing (valid, complete JSON; phrasing cut off mid-word) at 700
# tokens. A wider budget for this pass specifically, not a global bump
# sized for the worst case every pass pays for.
BROAD_MAX_TOKENS = 1800

NUMBER_TOKEN_RE = re.compile(r"\d+(?:[:.,]\d+)*")


def stable_id(prefix: str, *parts: str) -> str:
    """A deterministic, CONTENT-derived custom_id — never a positional
    index. The same logical candidate (e.g. the same real chunk_id +
    field label) hashes to the same id on every run, which is what lets
    checkpoint.load_done_custom_ids() recognize "already processed"
    correctly even when candidate ordering or the total candidate set
    shifts slightly between runs (Pass 2's live retrieval results in
    particular aren't guaranteed bit-identical run to run). A positional
    id (e.g. f"narrow-{index}") would silently misidentify a different
    candidate as "already done" the moment any earlier candidate in the
    list got skipped. Truncated hex digest to stay comfortably under the
    Batches API's custom_id length limit."""
    digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:20]
    return f"{prefix}-{digest}"


def build_client() -> anthropic.AsyncAnthropic:
    """Zero-arg construction — resolves ANTHROPIC_API_KEY (or an `ant auth
    login` profile) from the environment via the SDK's own precedence
    chain. Deliberately NOT routed through helpers/config.py's Settings:
    this is a scripts/ one-shot tool, not part of the live app's request
    path, and Settings' strict extra_forbidden validation already broke
    the running app once this session over an unrelated leftover .env
    variable — keeping this key out of that class entirely avoids
    repeating that failure mode for a value the live app has no use for.
    Async client, matching this whole pipeline's async convention
    (asyncpg, async controllers) — a blocking sync client would stall the
    event loop on every teacher call."""
    return anthropic.AsyncAnthropic()


@dataclass
class TeacherCallResult:
    raw_text: str
    input_tokens: int
    output_tokens: int
    stop_reason: str

    def truncated(self) -> bool:
        """True when the response was cut off by max_tokens rather than
        finishing naturally. This is INDEPENDENT of whether the JSON block
        happened to parse successfully — a response can be truncated
        entirely inside the phrasing that FOLLOWS a complete, valid JSON
        block, which parses fine and gives no other signal that anything
        is wrong (this pipeline's own --limit smoke test produced exactly
        that case). Callers must check this before trusting a record, not
        only check whether `extract_*` returned non-None."""
        return self.stop_reason != "end_turn"


@dataclass
class BatchRequestItem:
    """One pending distillation request, built entirely up front — no
    part of it depends on a prior teacher response, which is exactly what
    makes an entire sampling.py pass batchable in one shot rather than
    one live call at a time."""
    custom_id: str
    system_prompt: str
    user_message: str
    model: str = DEFAULT_MODEL
    max_tokens: int = DEFAULT_MAX_TOKENS


def _batch_request_dict(item: BatchRequestItem) -> dict:
    """Request/params shapes verified against the installed SDK's own
    source (types/messages/batch_create_params.py: `Request` is a plain
    TypedDict of {custom_id, params}; `params` is MessageCreateParamsNonStreaming
    — the same field names as messages.create()) rather than guessed."""
    return {
        "custom_id": item.custom_id,
        "params": {
            "model": item.model,
            "max_tokens": item.max_tokens,
            "system": item.system_prompt,
            "messages": [{"role": "user", "content": item.user_message}],
        },
    }


async def submit_batch(client: anthropic.AsyncAnthropic, items: list[BatchRequestItem]) -> str:
    """Submits every item as ONE Message Batch (50% off standard pricing)
    — the batch begins processing immediately server-side and can take up
    to 24 hours, though a batch this size typically finishes much faster.
    Returns the batch id to poll."""
    batch = await client.messages.batches.create(requests=[_batch_request_dict(item) for item in items])
    return batch.id


async def wait_for_batch(client: anthropic.AsyncAnthropic, batch_id: str, poll_interval_seconds: float = 30.0) -> None:
    """Polls processing_status until "ended", printing the live
    succeeded/errored/canceled/expired tallies each cycle so a long batch
    isn't a silent black box. `retrieve` is documented as the intended
    poll endpoint (idempotent, per the SDK's own docstring)."""
    while True:
        batch = await client.messages.batches.retrieve(batch_id)
        counts = batch.request_counts
        print(
            f"    [batch {batch_id}] status={batch.processing_status} "
            f"processing={counts.processing} succeeded={counts.succeeded} "
            f"errored={counts.errored} canceled={counts.canceled} expired={counts.expired}"
        )
        if batch.processing_status == "ended":
            return
        await asyncio.sleep(poll_interval_seconds)


async def collect_batch_results(client: anthropic.AsyncAnthropic, batch_id: str) -> dict[str, TeacherCallResult | None]:
    """{custom_id: TeacherCallResult}, or None for any request that didn't
    succeed (errored/canceled/expired) — callers treat a None exactly like
    a parse failure (skip, move on), the same discipline the notebook
    itself applies to a non-"stop" finish_reason. Results arrive in ANY
    order (the SDK's own docstring is explicit about this) — matched back
    to their request purely via custom_id, never by position."""
    results: dict[str, TeacherCallResult | None] = {}
    stream = await client.messages.batches.results(batch_id)
    async for entry in stream:
        if entry.result.type == "succeeded":
            message = entry.result.message
            raw_text = "".join(block.text for block in message.content if block.type == "text")
            results[entry.custom_id] = TeacherCallResult(
                raw_text=raw_text,
                input_tokens=message.usage.input_tokens,
                output_tokens=message.usage.output_tokens,
                stop_reason=message.stop_reason,
            )
        else:
            # errored / canceled / expired — Anthropic does not bill a
            # request that didn't succeed, so there is no usage to record.
            results[entry.custom_id] = None
    return results


async def execute_requests(
    client: anthropic.AsyncAnthropic, items: list["BatchRequestItem"], *, use_batch: bool, pass_label: str,
    resume_batch_id: str | None = None, on_batch_submitted=None,
) -> dict[str, "TeacherCallResult | None"]:
    """The single execution entry point every sampling.py pass calls —
    batches every item through the Message Batches API (50% off, the
    full-run default: `use_batch=(limit is None)`) or runs them one at a
    time through the live API (only for the `--limit` smoke-test path,
    where waiting on batch scheduling overhead for 2 rows is worse UX
    than the 2x per-token cost on a handful of calls). Callers get the
    same {custom_id: TeacherCallResult | None} shape either way and never
    branch on which path ran.

    resume_batch_id: when the caller's checkpoint state shows a batch
    already submitted for this pass before a crash/disconnect, pass its
    id here instead of `items` — this RECONNECTS (poll + collect) to that
    already-running, already-billed batch rather than either losing that
    work or paying twice by submitting a fresh duplicate.

    on_batch_submitted: called with the new batch_id the instant
    submit_batch() returns, before wait_for_batch's poll loop starts —
    lets the caller persist it to checkpoint state immediately, so even a
    crash one poll cycle later still has it recorded."""
    if resume_batch_id is not None:
        print(f"    [{pass_label}] resuming in-flight batch {resume_batch_id} — reconnecting, not resubmitting")
        await wait_for_batch(client, resume_batch_id)
        return await collect_batch_results(client, resume_batch_id)

    if not items:
        return {}

    if use_batch:
        batch_id = await submit_batch(client, items)
        print(f"    [{pass_label}] submitted batch {batch_id} ({len(items)} requests)")
        if on_batch_submitted is not None:
            on_batch_submitted(batch_id)
        await wait_for_batch(client, batch_id)
        return await collect_batch_results(client, batch_id)

    results: dict[str, TeacherCallResult | None] = {}
    for item in items:
        results[item.custom_id] = await call_teacher(
            client, system_prompt=item.system_prompt, user_message=item.user_message,
            model=item.model, max_tokens=item.max_tokens,
        )
    return results


async def call_teacher(
    client: anthropic.AsyncAnthropic,
    *,
    system_prompt: str,
    user_message: str,
    model: str = DEFAULT_MODEL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> TeacherCallResult:
    """Single live (non-batch) call — kept for the `--limit` smoke-test
    path, where waiting on batch scheduling overhead for 2 rows is worse
    UX than paying standard-not-batch rates on a handful of calls. Mirrors
    the notebook's own per-story call shape (cell-31), against Claude's
    Messages API instead of OpenAI's
    chat.completions. Never raises for a non-"end_turn" stop_reason or an
    unparseable response — callers check `.stop_reason` and the parsing
    helpers' None returns themselves and skip, matching the notebook's
    own `if finish_reason != "stop": continue` pattern rather than a hard
    failure that would abort the whole run over one bad example."""
    response = await client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )
    raw_text = "".join(block.text for block in response.content if block.type == "text")
    return TeacherCallResult(
        raw_text=raw_text,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        stop_reason=response.stop_reason,
    )


# ---------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------

DISTILLATION_ADDENDUM = "\n".join([
    "---",
    "أنت دلوقتي بتساعدي في توليد بيانات تدريب لنموذج آخر، مش بترديّ على "
    "مريض حقيقي مباشرة. رغم كده، اتبعي القواعد المذكورة فوق بالظبط في "
    "بناء أي رد. شكل ردك لازم يكون بالظبط الأجزاء المطلوبة في الـ TASK "
    "تحت، بالترتيب المذكور، من غير أي مقدمة أو تعليق قبلهم أو بعدهم.",
    "قاعدة أساسية لكل الأجزاء: أي بلوك JSON بتكتبيه لازم يحتوي فقط على "
    "المفاتيح المذكورة في 'JSON KEYS ALLOWED' — ممنوع نهائيًا تخترعي "
    "مفتاح جديد، تترجميه، أو تغيّري صياغته. القيم كمان لازم تتنسخ حرفيًا "
    "من الـ CONTEXT، من غير أي تقريب أو إعادة صياغة.",
    "أي جزء 'نص طبيعي' (الرد بالعامية) لازم يكون مبني فقط على المعلومات "
    "الموجودة في بلوك الـ JSON اللي كتبتيه هي نفسها — ممنوع تضيفي أي "
    "حقيقة، رقم، أو تفصيلة مش موجودة فيه.",
])


def build_teacher_system_prompt(base_directive: str) -> str:
    """base_directive is the REAL, unchanged whatsapp_mode_a_reply_directive
    text (resolved via TemplateParser by the caller, never retyped here).
    The addendum below is intentionally NOT part of what gets saved into
    a training record's `system` field (see format_alpaca.py) — the
    fine-tuned model only ever sees the pure real directive at inference,
    so that's the only thing that belongs in the saved `system` field.
    This combined prompt exists solely to steer the teacher's own output
    shape during generation."""
    return base_directive + "\n\n" + DISTILLATION_ADDENDUM


# ---------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------

GENERATE_QUESTION_TASK = "\n".join([
    "## TASK:",
    "اخترعي سؤال طبيعي واحد بس، بصياغة مختلفة في كل مرة، ممكن مريض حقيقي "
    "يبعته على واتساب عن حاجة موجودة (أو مش موجودة، حسب الحالة) في الـ "
    "CONTEXT ده — عامية مصرية طبيعية زي ما مريض حقيقي هيكتب، مش صيغة "
    "رسمية. بعدين جاوبي على السؤال ده بنفس القواعد المذكورة فوق. اكتبي "
    "ردك بالظبط بالشكل ده:",
    "",
    "## PATIENT QUESTION:",
    "<السؤال هنا>",
    "",
    "## JSON:",
    "```json",
    "<JSON هنا>",
    "```",
    "",
    "<الصياغة الطبيعية هنا>",
])

ANSWER_GIVEN_QUESTION_TASK = "\n".join([
    "## TASK:",
    "جاوبي على 'PATIENT MESSAGE' تحت، باستخدام الـ CONTEXT المتاح فقط، "
    "بنفس القواعد المذكورة فوق. اكتبي ردك بالظبط بالشكل ده:",
    "",
    "## JSON:",
    "```json",
    "<JSON هنا>",
    "```",
    "",
    "<الصياغة الطبيعية هنا>",
])


def build_generation_prompt(*, context_text: str, allowed_keys_description: str) -> str:
    """Pass 1 (Narrow) / Pass 2 (Broad) Positive: the teacher invents the
    patient question itself. A hardcoded Python question template per
    field label isn't viable here (16 real sheets' worth of real Arabic
    field names, unknown at code-writing time — and a fixed template
    string would itself violate the no-hardcoding rule) and wouldn't
    serve anti-memorization as well as a genuinely-varied, LLM-generated
    question does."""
    return "\n\n".join([
        f"## CONTEXT:\n{context_text}",
        f"## JSON KEYS ALLOWED (المفتاح لازم يكون واحد بالظبط من دول، منسوخ حرفيًا):\n{allowed_keys_description}",
        GENERATE_QUESTION_TASK,
    ])


def build_generation_prompt_for_missing_field(*, context_text: str, missing_field: str) -> str:
    """Pass 4a (Ambiguous — direct/blank field): the CONTEXT is a real
    row's real field_data, but explicitly does NOT include `missing_field`
    (schema_registry's discovered columns for this sheet show it exists
    for other rows, just not this one — see sampling.py's
    _run_ambiguous_direct). The teacher is told the field is absent so it
    can invent a question specifically targeting it and correctly refuse,
    rather than guessing a question at random and only sometimes hitting
    the gap."""
    return "\n\n".join([
        f"## CONTEXT:\n{context_text}",
        f"## ملحوظة: المعلومة عن '{missing_field}' مش موجودة في الـ CONTEXT ده لهذا الصف بالذات "
        "(موجودة لصفوف تانية في نفس الشيت، لكن مش هنا).",
        "## JSON KEYS ALLOWED: {} — لازم يكون JSON فاضي بالظبط، لأن المعلومة المطلوبة مش متاحة.",
        "## TASK:",
        f"اخترعي سؤال طبيعي واحد ممكن مريض يسأله تحديدًا عن '{missing_field}' للصف ده، بعدين ردي "
        "بإنها مش متاحة/محتاجة تأكيد من موظف — أبدًا تخمين. اكتبي ردك بالظبط بالشكل ده:",
        "",
        "## PATIENT QUESTION:",
        "<السؤال هنا>",
        "",
        "## JSON:",
        "```json",
        "{}",
        "```",
        "",
        "<الصياغة الطبيعية هنا>",
    ])


def build_generation_prompt_for_conflict(*, context_text: str, label: str) -> str:
    """Pass 4b (Ambiguous — cross-chunk conflict): two real sources
    reporting genuinely different values under the same field label (see
    sampling.py's _run_ambiguous_cross_chunk). The teacher is told this
    explicitly and must escalate rather than pick one value arbitrarily."""
    return "\n\n".join([
        f"## CONTEXT:\n{context_text}",
        f"## ملحوظة: المصادر فوق بتقول قيم مختلفة لنفس المعلومة ('{label}') — تعارض حقيقي، مش خطأ.",
        "## JSON KEYS ALLOWED: {} — لازم يكون JSON فاضي بالظبط، لأن المعلومة متعارضة ومحتاجة تأكيد.",
        "## TASK:",
        f"اخترعي سؤال طبيعي واحد ممكن مريض يسأله عن '{label}' هنا، بعدين ردي بإن المعلومة محتاجة "
        "تأكيد من موظف لأنها مش واضحة/متعارضة — أبدًا تختاري قيمة واحدة من الاتنين بنفسك. اكتبي "
        "ردك بالظبط بالشكل ده:",
        "",
        "## PATIENT QUESTION:",
        "<السؤال هنا>",
        "",
        "## JSON:",
        "```json",
        "{}",
        "```",
        "",
        "<الصياغة الطبيعية هنا>",
    ])


def build_verification_prompt(*, question: str, context_text: str, allowed_keys_description: str) -> str:
    """Pass 3 (Absence): the question is already given (sampled from Pass
    1/2's real question pool), deliberately paired with unrelated
    context. Only JSON + phrasing are requested — no question-invention
    section."""
    return "\n\n".join([
        f"## CONTEXT:\n{context_text}",
        f"## PATIENT MESSAGE:\n{question}",
        f"## JSON KEYS ALLOWED (المفتاح لازم يكون واحد بالظبط من دول، منسوخ حرفيًا):\n{allowed_keys_description}",
        "## ملحوظة: لو الـ CONTEXT مفيهوش إجابة حقيقية للسؤال ده، اطلعي JSON فاضي {} بالظبط.",
        ANSWER_GIVEN_QUESTION_TASK,
    ])


# ---------------------------------------------------------------------
# Response parsing — robust, dependency-free (no json_repair install
# needed; the notebook's own reliance on it is an OpenAI-quirk
# workaround this doesn't need to inherit).
# ---------------------------------------------------------------------

def _extract_fenced_or_balanced_json(text: str) -> tuple[object | None, int]:
    """Returns (parsed_json_or_None, end_index_in_text). Prefers an
    explicit ```json ... ``` fence; falls back to a manual bracket-balance
    scan from the first '{' or '[' so a teacher response that skips the
    fence still parses."""
    fence_match = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
    if fence_match:
        try:
            return json.loads(fence_match.group(1)), fence_match.end()
        except json.JSONDecodeError:
            return None, 0

    start = None
    for i, ch in enumerate(text):
        if ch in "{[":
            start = i
            break
    if start is None:
        return None, 0

    open_ch, close_ch = text[start], ("}" if text[start] == "{" else "]")
    depth = 0
    for i in range(start, len(text)):
        if text[i] == open_ch:
            depth += 1
        elif text[i] == close_ch:
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1]), i + 1
                except json.JSONDecodeError:
                    return None, 0
    return None, 0


def extract_json_and_phrasing(raw_text: str) -> tuple[object | None, str]:
    """For the 2-part (## JSON / phrasing) response shape — Pass 3 and
    every Pass 4 sub-case use this."""
    text = raw_text.strip()
    json_marker = text.find("## JSON:")
    if json_marker != -1:
        text = text[json_marker + len("## JSON:"):].strip()

    json_block, end_index = _extract_fenced_or_balanced_json(text)
    if json_block is None:
        return None, ""
    return json_block, text[end_index:].strip()


def extract_question_json_and_phrasing(raw_text: str) -> tuple[str | None, object | None, str]:
    """For the 3-part (## PATIENT QUESTION / ## JSON / phrasing) response
    shape — Pass 1 and Pass 2 Positive, plus the Pass 4 sub-cases that
    also ask the teacher to invent the question."""
    text = raw_text.strip()

    question_marker = text.find("## PATIENT QUESTION:")
    json_marker = text.find("## JSON:")
    if question_marker == -1 or json_marker == -1 or json_marker <= question_marker:
        return None, None, ""

    question = text[question_marker + len("## PATIENT QUESTION:"):json_marker].strip()
    if not question:
        return None, None, ""

    json_block, phrasing = extract_json_and_phrasing(text[json_marker:])
    return question, json_block, phrasing
