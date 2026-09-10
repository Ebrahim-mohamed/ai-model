import json
import logging
import re

from .BaseController import BaseController
from .ReplyVerificationController import REPLY_VERIFICATION_FAILED_FALLBACK
from helpers.bidi_text import normalize_bilingual_punctuation
from stores.llm.templates.template_parser import TemplateParser, TemplateBucket
from stores.generation.GenerationInterface import GenerationTimeoutError

# Real, human-facing Arabic label per real brand code — shared by
# _mode_b_substitutions' own $brand Bucket B placeholder AND the Dynamic
# Cross-Brand Availability Pipeline below (2026-09-08), rather than two
# separate copies of the same mapping drifting apart. "رايلاب" (the
# center's own name, not a specific brand) is the deliberate fallback for
# any brand code neither of these two real, current brands — same
# fallback _mode_b_substitutions already used before this was extracted.
_BRAND_LABELS = {"cairoscan": "كايروسكان", "technoscan": "تكنوسكان"}

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

# Same "not Bucket B/C" carve-out as GENERATION_UNAVAILABLE_FALLBACK above
# — a corrective retry instruction, not real transcribed business content.
# 2026-09-02 audit finding: on a broad turn with several closely-clustered
# candidate sources (e.g. 5 topically-adjacent MRI-brain exam variants),
# the model was observed returning completely empty `fields` for every
# source in its JSON block while its own phrasing still correctly quoted
# a real fact from CONTEXT — reproduced twice on identical real traffic.
# Broad breadth already shows the model the maximal candidate set this
# turn's retrieval produced (unlike narrow, which can widen to a real
# superset — see the widening-retry block in _mode_a_reply), so there's
# no additional real evidence to add on retry; this nudge instead retries
# the SAME context once with an explicit instruction naming the exact
# failure pattern observed, asking the model to select and populate the
# right source's fields before writing the phrasing.
_BROAD_EMPTY_JSON_RETRY_NUDGE = (
    "تنبيه: في المحاولة اللي فاتت رجّعت حقول الـ JSON فاضية لكل المصادر "
    "رغم إن فيه مصدر واحد على الأقل بيجاوب على السؤال. لازم تحدد المصدر "
    "الصح وتملي حقوله (fields) بالمعلومات اللي هتستخدمها فعلا في ردك "
    "النصي، قبل ما تكتب الرد."
)

# Dynamic Cross-Brand Availability Pipeline (2026-09-08 — re-implemented
# with a deterministic Hallucination Lock after real evidence, twice
# over, showed the fine-tuned model confidently claiming a service was
# available while its own CONTEXT explicitly said otherwise, EVEN when
# told directly via a cross-brand note. See _is_placeholder_shaped_chunk,
# _mode_a_reply's own availability_status handling, and
# ReplyVerificationController.verify_and_gate's new `required_phrase`
# param for the three layers this pipeline is built from.

# Structural (not textual) floor for _is_placeholder_shaped_chunk's own
# distinct-value-ratio check to be statistically meaningful at all — a
# chunk with only 2-3 real fields can trivially show a low distinct-ratio
# by chance (e.g. two fields that happen to share one real value), which
# would false-positive on small, legitimate chunks having nothing to do
# with a "not available under this brand" redirect row. This is a
# mathematical necessity for the ratio metric itself, not a tunable
# business policy — kept as a plain constant rather than a client_config
# column for that reason (claude.md §1.3 draws this same "structural
# constant vs. real business threshold" line for other pure-math values
# elsewhere in this codebase).
_PLACEHOLDER_CHECK_MIN_FIELDS = 5

# Availability status labels — plain strings, not an Enum, matching this
# file's own existing convention for breadth ("narrow"/"broad" are also
# plain strings, never an Enum) so every log line stays trivially
# grep-able and directly comparable in this method's own existing log
# style, without an extra .value indirection.
_AVAILABILITY_NORMAL = "normal"
_AVAILABILITY_UNAVAILABLE_HERE = "unavailable_here_check_other_brand"
_AVAILABILITY_UNAVAILABLE_EVERYWHERE = "unavailable_everywhere"

# Dynamic Disambiguation / Clarification Flow (2026-09-08). Real incident:
# a genuinely ambiguous query ("عايز اعمل اشعة CBCT", no sub-type stated)
# retrieved a real, correct top-1 chunk (CBCT (3D) Single Arch) that Mode A
# grounded on and answered confidently — technically not wrong (Single Arch
# WAS the top-ranked real candidate), but the patient never said which of
# the catalog's 8 real CBCT sub-variants (Single Arch, Both Arches,
# Craniofacial, PNS, Quadrent, TMJ Both/One Side, segment) they meant, and
# a separate, independent signal (the analytics classify_topic call, blind
# to what was actually retrieved) guessed a DIFFERENT sibling entirely
# (Both Arches) — real, confirmed evidence the query was ambiguous enough
# that two independent processes couldn't agree on which real variant was
# meant.
#
# This is deliberately NOT solved by asking the model to notice ambiguity
# in its own CONTEXT: _narrow_context_block shows the model exactly ONE
# chunk (single-chunk-always — see _mode_a_reply's own docstring for why
# the earlier multi-chunk "broad" CONTEXT design was retired: it broke the
# fine-tuned model's own JSON-selection step even when the right chunk was
# unambiguously #1). CONTEXT structurally cannot "contain multiple
# variants" under this design, so a prompt instruction about CONTEXT
# containing several options would have nothing real to act on.
#
# Detection instead runs in Python, on `all_results` — the broad-ceiling
# candidate set _mode_a_reply already fetches for breadth classification,
# no new retrieval call — using the exact same "structural, not textual"
# philosophy _is_placeholder_shaped_chunk already established: real
# distinct display names (see _extract_display_name) among the top real
# candidates, sharing a genuine common leading-token prefix, at closely
# clustered real scores, is evidence of genuine competing sub-variants —
# generalizes to any exam/lab-test/package family, zero business-name
# hardcoding anywhere in this mechanism.
_CLARIFICATION_MIN_DISTINCT_NAMES = 2  # structural minimum for "multiple" — not a tunable business threshold

# Field-name-key heuristic (2026-09-08) — same one validated against this
# project's real corpus in scripts/extract_topic_taxonomy.py (542/542
# offering chunks matched, 0 unmatched): a field_data key starting with
# "اسم" ("name"), once ARABIC TATWEEL is stripped, reliably holds the real
# display name across every real offering sheet checked (Examinations'
# "اسم الفحص", تحاليل's "اسم التحليل", باقات التحاليل's "اسم الباقه ...").
# This is the SAME real, targeted lesson this file's own Layer 3 fallback
# comment above already draws from "the diversity-heuristic finding from
# the clarifying-question-pathway work" — the earlier "just grab the first
# populated field" heuristic was unreliable across differently-shaped
# sheets; matching on a real, shared KEY PATTERN (not "whichever field
# happens to be first") is what actually generalizes.
_DISPLAY_NAME_KEY_PREFIX = "اسم"

# Category/modality key heuristic (2026-09-08 addendum — MRI generalization
# fix). Same "match a real, shared KEY PATTERN, never a hardcoded literal"
# discipline as _DISPLAY_NAME_KEY_PREFIX above, applied to a SECOND real,
# structurally-consistent field: "نوع" ("type"), confirmed present across
# every real Examinations row sampled this session regardless of modality
# (نوع الاشعه: CT / MRI / CBCT / X-Ray / Doppler alike). Real, confirmed
# finding this addresses: _common_leading_prefix over اسم الفحص values
# ONLY works when a sheet's own naming convention happens to repeat the
# modality inside the name field itself (CBCT's real rows do: "CBCT (3D)
# Single Arch", "CBCT (3D) Both Arches"). MRI's real rows do NOT — the
# modality lives ONLY in نوع الاشعه, while اسم الفحص is just the body part
# ("Brain", "Neck", "Abdomen" — verified directly against 30 real MRI rows,
# zero shared leading token found). Matching "نوع"-prefixed keys generically
# (not "نوع الاشعه" as a literal string) lets this also pick up a
# differently-spelled category field on a sheet never explicitly checked
# (e.g. a hypothetical "نوع التحليل" on a lab-package sheet), the same
# generalization argument _DISPLAY_NAME_KEY_PREFIX already relies on.
_CATEGORY_KEY_PREFIX = "نوع"


def _extract_display_name(chunk) -> str | None:
    """Returns the first field_data value whose (tatweel-stripped) key
    starts with _DISPLAY_NAME_KEY_PREFIX, or None if this chunk has no
    such field (e.g. a Branch Directory/Insurance Guide row, or any other
    non-offering sheet — see _detect_variant_ambiguity's own docstring for
    why that's a correct, not a missing, case)."""
    field_data = (chunk.metadata_payload or {}).get("field_data") or {}
    for key, value in field_data.items():
        if _TATWEEL_RE.sub("", key).strip().startswith(_DISPLAY_NAME_KEY_PREFIX) and value and str(value).strip():
            return re.sub(r"\s+", " ", str(value)).strip()
    return None


def _extract_category(chunk) -> str | None:
    """Same mechanism as _extract_display_name, matching
    _CATEGORY_KEY_PREFIX ("نوع") instead — returns the real modality/
    category value (e.g. "MRI الرنين المغناطيسي") a chunk's own field_data
    carries, or None if it has no such field. See _CATEGORY_KEY_PREFIX's
    own comment for the real finding this exists to address."""
    field_data = (chunk.metadata_payload or {}).get("field_data") or {}
    for key, value in field_data.items():
        if _TATWEEL_RE.sub("", key).strip().startswith(_CATEGORY_KEY_PREFIX) and value and str(value).strip():
            return re.sub(r"\s+", " ", str(value)).strip()
    return None


def _common_leading_prefix(token_lists: list[list[str]]) -> list[str]:
    """Longest shared leading-token sequence across every list in
    `token_lists` — e.g. [["CBCT","(3D)","Single","Arch"],
    ["CBCT","(3D)","Both","Arches"]] -> ["CBCT","(3D)"]. Empty list if
    the very first token already differs, or if any list is itself empty."""
    if not token_lists or any(not tokens for tokens in token_lists):
        return []
    prefix = []
    for position, first_token in enumerate(token_lists[0]):
        if all(len(tokens) > position and tokens[position] == first_token for tokens in token_lists):
            prefix.append(first_token)
        else:
            break
    return prefix


def _group_by_category(candidates: list[tuple[str, str | None, float]]) -> tuple[str, list[str]] | None:
    """Primary grouping strategy (2026-09-08, MRI generalization fix) —
    `candidates` is a list of (name, category, score) for real,
    score-gap-qualifying, distinct-named results. Returns (parent_category,
    [full name per variant]) if EVERY candidate carries a real category
    value (_extract_category) and they all share the SAME one (normalized)
    — a real, retrieval-independent signal that they're the same family,
    regardless of whether their own display names share any text at all
    (MRI's real rows don't — "Brain" vs "Neck" vs "Abdomen" — the family
    signal lives only in نوع الاشعه). Returns None if any candidate lacks a
    category value, or their categories disagree — the caller falls back
    to prefix-based grouping in either case, never guesses.

    Unlike prefix-based grouping (which strips a shared prefix to build
    each suffix), the full real name is shown per option here — there is
    no shared name-text to strip, and stripping would risk leaving an
    empty or misleading fragment for a name that doesn't happen to start
    with any of the modality wording at all."""
    categories = {_normalize_display_text(category) for _name, category, _score in candidates if category}
    if len(categories) != 1 or any(category is None for _name, category, _score in candidates):
        return None

    parent_category = next(category for _name, category, _score in candidates if category)
    suffixes = [name for name, _category, _score in candidates]
    return parent_category, suffixes


def _group_by_name_prefix(candidates: list[tuple[str, str | None, float]]) -> tuple[str, list[str]] | None:
    """Fallback grouping strategy — the ORIGINAL 2026-09-08 mechanism,
    unchanged: real, non-empty common leading-token prefix across the
    candidates' own display names (_common_leading_prefix). Used only when
    _group_by_category can't decide (a category field is absent on at
    least one real candidate, or a sheet's own category values genuinely
    disagree) — this is what still correctly handles CBCT, whose real rows
    happen to repeat "CBCT (3D)" inside اسم الفحص itself, independent of
    whatever نوع الاشعه says."""
    names = [name for name, _category, _score in candidates]
    token_lists = [name.split() for name in names]
    prefix_tokens = _common_leading_prefix(token_lists)
    if not prefix_tokens:
        return None

    parent_category = " ".join(prefix_tokens)
    suffixes = []
    for tokens in token_lists:
        suffix = " ".join(tokens[len(prefix_tokens):]).strip()
        if suffix:
            suffixes.append(suffix)

    # Every real candidate collapsed to an empty suffix (i.e. the
    # "distinct" names only differed by whitespace/case, already
    # normalized away upstream) — not real variants, nothing to
    # disambiguate.
    if len(suffixes) < _CLARIFICATION_MIN_DISTINCT_NAMES:
        return None

    return parent_category, suffixes


def _normalize_display_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


_MIN_TOKEN_LENGTH = 2  # structural noise floor -- a single LETTER carries no real distinguishing signal

# Punctuation to treat as a token boundary, not part of a token -- 2026-09-08
# addendum, found by hand-testing the query-specificity gates against real
# cases, not anticipated upfront: a candidate suffix like "2 (مدرسة
# التوفيقية)" tokenizes to "...التوفيقية)" (closing paren still attached) on
# a plain whitespace split, which then never equals the query's own bare
# "التوفيقية" token -- a real, confirmed false negative on the Shubra
# branch case. Replaced with a space (not deleted outright) so "2(مدرسة"
# doesn't glue into one token either.
_TOKEN_PUNCTUATION_RE = re.compile(r"[؟،؛!.,()\[\]{}\"'/\\-]")

# Hamza-bearing alif forms -- 2026-09-08 addendum, same "found by hand-
# testing" story: the real corpus spells "الاسنان" (dental) without a
# hamza in one place and a patient query naturally writes "الأسنان" with
# one, or vice versa -- exact-string token comparison silently missed the
# CBCT case's own category-overlap check for exactly this reason. Folding
# every hamza-bearing alif to a bare alif is standard, minimal Arabic
# search normalization (not a custom invention for this project), applied
# only to this token-comparison step -- never touches what's stored or
# what's shown to the patient.
_HAMZA_ALIF_RE = re.compile(r"[أإآ]")

# Attached single-letter conjunctions/prepositions (و ف ب ل ك) and the
# definite article (ال), including the ل+ال orthographic contraction
# ("لل" — "the X" turning into "for/to the X" glues to "للX", the alif of
# ال is elided, not just optionally present) -- 2026-09-08 addendum, the
# third real gap hand-testing found: "الرنين"/"المقطعية"/"للشرايين" in real
# retrieved text never string-match the query's own bare "رنين"/"مقطعية"/
# "شرايين" without stripping this attached prefix first. Checked longest-
# first so "بال"/"لل" aren't left partially stripped by the shorter "ب"/"ل"
# alternative matching first. This is standard, minimal Arabic light-
# stemming (not a custom invented rule), applied only at comparison time.
_ARABIC_ATTACHED_PREFIXES = ("بال", "وال", "فال", "كال", "لل", "ال", "و", "ف", "ب", "ل", "ك")


def _strip_arabic_prefix(token: str) -> str:
    for prefix in _ARABIC_ATTACHED_PREFIXES:
        if token.startswith(prefix) and len(token) > len(prefix) + 1:
            return token[len(prefix):]
    return token


def _tokenize(text: str) -> set[str]:
    """Real, meaningful tokens only -- normalized (tatweel-stripped,
    whitespace-collapsed, lowercased via _normalize_display_text;
    punctuation-separated, hamza-folded, and attached-prefix-stripped per
    the three comments above) and length-filtered to drop single-
    character noise. Shared by every dynamic query-specificity check
    below -- one tokenization rule, not a separate ad-hoc split per
    helper. All three normalization steps were added after real failures
    surfaced during regression testing against the actual 14 benchmark
    cases, not designed upfront -- disclosed, real Arabic-text-comparison
    limitations, same "starting point, not perfect" status every other
    heuristic in this project carries; a genuinely novel attached-prefix
    or spelling variant not covered by these specific patterns can still
    cause a real false negative (a legitimate case failing to suppress,
    or a false positive staying unsuppressed).

    2026-09-09 fix -- real, confirmed incident: the length filter dropped
    a bare single-digit token ("6") from BOTH the query's own tokens and
    a candidate's distinguishing tokens, for an EEG-duration family whose
    ONLY real distinguishing content between siblings ("6 hours" vs "7
    hours" vs "4 hours") IS a single digit. The patient's own follow-up
    ("رسم مخ مطول لمدة 6 ساعات", real retrieval score 0.998 -- correctly,
    confidently the right chunk) still re-triggered clarification, because
    _query_already_specifies_one had nothing left to match against once
    "6" was silently discarded from both sides of the comparison. A bare
    digit is a highly meaningful, common distinguishing signal for exactly
    this "duration/count" shape of variant family -- unlike a single
    Arabic/English LETTER (which usually is real noise: a stray attached
    particle fragment), a single digit essentially never is. Digits are
    therefore exempt from the length floor; the floor still applies to
    everything else exactly as before."""
    text = _TOKEN_PUNCTUATION_RE.sub(" ", text)
    text = _HAMZA_ALIF_RE.sub("ا", text)
    tokens = set()
    for token in _normalize_display_text(text).split():
        token = _strip_arabic_prefix(token)
        if len(token) >= _MIN_TOKEN_LENGTH or token.isdigit():
            tokens.add(token)
    return tokens


_GROUNDING_NOTE_PREFIX = (
    "ملحوظة داخلية (من سياق المحادثة، وليست كلام المريض الحرفي) -- "
    "المقصود بالتحديد من رسالة المريض هو: "
)


def _build_specificity_grounding_note(text: str, resolved_query: str) -> str | None:
    """Dynamic, domain-agnostic Option A fix (2026-09-09) for a real,
    confirmed generation failure: `_generate_grounded_reply` deliberately
    shows the model the patient's own literal, unmodified `text` as
    PATIENT MESSAGE: (see that method's own docstring, and _mode_a_reply's
    -- so a short reply like "اه" never gets phrased as if the patient had
    typed the rewritten query), while retrieval is driven entirely by
    `resolved_query` (CQR's history-aware rewrite, which can pull forward
    real specificity the current message alone never states -- an earlier
    turn's exam variant, branch, or duration).

    Real incident this closes: patient's literal current message was
    'عايز اعمل رسم مخ، وهياخد وقت قد ايه؟' (generic, no duration
    mentioned); CQR correctly resolved it to 'عايز اعمل رسم مخ مطول لمدة 6
    ساعات، هياخد وقت قد ايه؟' using this session's own earlier turns, and
    retrieval correctly grounded on the single real 6-hour EEG chunk
    (score 0.89) -- but generation, shown only the generic literal
    message, had no cue that "6 hours" (sitting right in the retrieved
    chunk's own اسم الفحص field) was the specific thing being asked about,
    and defaulted to declining with a human-handoff offer instead of
    stating it.

    Deliberately NOT a hardcoded duration/EEG/exam check: this compares
    `resolved_query`'s real tokens against `text`'s real tokens (the same
    domain-agnostic _tokenize every other dynamic check in this module
    already uses -- hamza-folded, prefix-stripped, digit-exempt) and fires
    on ANY case where CQR added real content the patient's literal message
    didn't itself contain, regardless of whether that content is a
    duration, a branch name, an exam variant, or a pronoun resolved into a
    proper noun -- the same class of gap, whatever the surface content.
    Returns None (no note -- and therefore zero prompt-shape change) when
    resolved_query adds nothing real over text, which is the common case
    for a self-contained first turn.

    Surfaces the FULL resolved_query, not just the raw token diff --
    Arabic word order carries real grammatical meaning a bag of tokens
    would destroy, so the model is given one coherent sentence to read,
    clearly labelled as conversational context rather than a new fact to
    invent, never as something to treat as the patient's own quoted
    words (chat_history/human_handoff_queue/ReplyVerificationController
    all still see the real, unmodified `text` -- this note is generation-
    prompt-only, appended to CONTEXT: by _generate_grounded_reply's own
    caller, never written back to persisted state)."""
    if not resolved_query:
        return None
    resolved_query = resolved_query.strip()
    if not resolved_query or resolved_query == text.strip():
        return None
    extra_tokens = _tokenize(resolved_query) - _tokenize(text)
    if not extra_tokens:
        return None
    return f"{_GROUNDING_NOTE_PREFIX}{resolved_query}"


def _category_overlaps_query(parent_category: str, resolved_query: str) -> bool:
    """Coarse, cheap gate (2026-09-08, clarification false-positive fix)
    -- does the query share ANY real token with the cluster's own
    parent_category at all? Real, confirmed incidents this closes:
    queries about branch equipment, anesthesia policy, and loyalty-card
    terms retrieved several real-but-irrelevant Examinations rows that
    happened to cluster together, and got offered as a clarification menu
    for a question that was never about picking an exam variant in the
    first place -- e.g. "خدمة التخدير الكلي متاحة في فرع الحوامدية؟" (a
    pure anesthesia-policy question) sharing zero real tokens with the
    "MRI الرنين المغناطيسي" category its spuriously-retrieved candidates
    happened to share. Domain-agnostic: operates on whatever
    parent_category string category-grouping or prefix-grouping produced,
    never a hardcoded category/domain name.

    Disclosed, NOT fully solved by this gate alone: a query that genuinely
    does mention the right category but is really asking a different KIND
    of question about it (e.g. "لو عايز اضيف رسم عصب بالإبرة... هياخد فلوس
    زياده؟" -- an EMG pricing question, not "which EMG exam") still passes
    this gate, since it does share real tokens with "EMG رسم العصب". That
    residual gap is a retrieval-recall problem (the real pricing-policy
    chunk was never retrieved), not something a text-overlap gate can
    fix -- left open, not silently claimed solved."""
    return bool(_tokenize(parent_category) & _tokenize(resolved_query))


def _distinguishing_tokens(
    candidate_suffix: str, sibling_suffixes: list[str], category_tokens: frozenset[str] = frozenset(),
) -> set[str]:
    """Real tokens in `candidate_suffix` that do NOT appear in ANY sibling
    in `sibling_suffixes`, AND are not themselves part of `category_tokens`
    -- plain set difference, no domain knowledge of what an "exam",
    "branch", or "package" is. A token unique to one candidate within its
    own cluster is real, structural evidence that a query mentioning it
    picks out THAT candidate specifically, not just the shared family.

    `category_tokens` exclusion (2026-09-08 addendum, found the same way
    as the tokenizer fixes -- real regression-suite failure, not
    anticipated upfront): a candidate's own name can restate a word that's
    really just describing the shared family, not a genuine distinguishing
    detail -- real case: "MRI Brain رنين على المخ" literally contains
    "رنين" (the generic Arabic word for the MRI modality itself), which its
    siblings "MRV Brain اورده المخ"/"MRA Brain شرايين المخ" simply don't
    happen to repeat verbatim (they use the English MRV/MRA abbreviation
    instead) -- but "رنين" is ALSO literally part of the shared
    parent_category ("MRI الرنين المغناطيسي"), so its absence from the
    other two suffixes is coincidental spelling, not a real distinguishing
    fact. Excluding any token already present in the category label closes
    this without needing to know anything about MRI/MRV/MRA specifically."""
    sibling_tokens: set[str] = set()
    for sibling in sibling_suffixes:
        sibling_tokens |= _tokenize(sibling)
    return _tokenize(candidate_suffix) - sibling_tokens - category_tokens


def _query_already_specifies_one(resolved_query: str, candidates: list[str], parent_category: str) -> bool:
    """True iff the query's own real tokens match exactly ONE candidate's
    distinguishing tokens (2026-09-08, clarification false-positive fix)
    -- real, confirmed incidents this closes: "فرع شبرا التوفيقية..."
    already naming a specific branch, "هولتر 72 ساعة" / "الماموجرام
    الديجيتال" / "ديناميكية تبول" already naming a specific exam variant,
    all still triggered clarification under the score-gap check alone,
    at real retrieval scores of 0.94-0.998 -- the top candidate was
    already correctly, confidently ranked #1, nothing here needed to be
    re-ranked, only the clarification trigger itself needed suppressing.

    Matches on RELATIVE distinctiveness (a token unique within this
    specific cluster, via _distinguishing_tokens), not an absolute token
    count -- an absolute-count rule ("at least 2 matching tokens") was
    tried first and found, by hand-checking against these exact real
    cases, to under-fire on a single-distinguishing-token candidate
    ("الديجيتال" alone correctly identifies Digital Mammogram against its
    siblings) while a fixed low count risks over-firing elsewhere.
    Relative distinctiveness needs no count threshold to tune at all --
    it only asks "is this word specific to one candidate in front of us
    right now," which generalizes identically whether the candidates are
    exam variants, branches, or packages.

    Disclosed, real limitation (found by hand-checking, not theoretical):
    a query using a broad/generic term that's ALSO structurally unique
    within one specific cluster (e.g. "مقطعية الأمعاء" against
    "Enterography الامعاء" vs "مقطعيه على القولون colonography") can still
    false-suppress a genuinely open-ended query -- this check cannot
    distinguish "the patient specifically selected this option" from "the
    patient used a generic word that happens not to appear in the
    sibling's name either." No purely structural, non-semantic check
    resolves this perfectly; flagged for the regression suite to actually
    catch, never silently assumed solved.

    `parent_category`'s own tokens are excluded from every candidate's
    distinguishing set (see _distinguishing_tokens' own comment on the
    real MRI Brain/MRV/MRA incident this addendum closes)."""
    query_tokens = _tokenize(resolved_query)
    category_tokens = frozenset(_tokenize(parent_category))
    matched = []
    for index, candidate in enumerate(candidates):
        siblings = candidates[:index] + candidates[index + 1:]
        if _distinguishing_tokens(candidate, siblings, category_tokens) & query_tokens:
            matched.append(candidate)
    return len(matched) == 1


def _detect_variant_ambiguity(
    all_results: list[dict], client_config, resolved_query: str,
) -> tuple[str, list[str]] | None:
    """Returns (parent_category, [option per real distinct variant]) if
    the top real candidates in `all_results` (already sorted by score
    descending) represent genuinely competing sub-variants of the same
    real family AND `resolved_query` doesn't already specify which one,
    else None. Checked in this order:

    1. At least _CLARIFICATION_MIN_DISTINCT_NAMES distinct real display
       names (via _extract_display_name), each within
       client_config.whatsapp_variant_ambiguity_score_gap of the top
       score — a real, clear single winner (large gap to the next
       distinct name) is NOT ambiguous, matching _classify_breadth's own
       established gap-based reasoning for the identical kind of
       decision. A chunk with no display name at all (a chunk type with
       no matching field — never filtered by chunk_type itself, this
       stays domain-agnostic by construction) is simply skipped, not
       treated as a blocking failure.

    2. Those distinct-named candidates group into a real family — tried
       via TWO independent strategies, category-based first
       (_group_by_category), falling back to name-prefix-based
       (_group_by_name_prefix) only when category grouping can't decide.
       See each helper's own docstring for why: CBCT's real rows repeat
       the modality inside اسم الفحص itself (prefix grouping works, no
       category needed); MRI's real rows only carry the modality in the
       separate نوع الاشعه field (verified: 30 real MRI رows, zero shared
       leading token in اسم الفحص — prefix grouping alone silently missed
       every real MRI ambiguity case until this fix). Neither strategy
       hardcodes a modality name anywhere — both match on real, generic
       KEY PATTERNS (_DISPLAY_NAME_KEY_PREFIX / _CATEGORY_KEY_PREFIX),
       never a literal like "MRI" or "CBCT".

    3. The resulting parent_category shares at least one real token with
       `resolved_query` (_category_overlaps_query) — a query that never
       mentioned this family at all is almost certainly asking about
       something retrieval failed to surface, not choosing among these
       candidates (real, confirmed incidents: anesthesia-policy and
       loyalty-card questions spuriously clustering with unrelated MRI/
       Doppler exam rows).

    4. `resolved_query` does NOT already specify exactly one candidate
       (_query_already_specifies_one returning True suppresses) — 2026-
       09-08 correction: an earlier version of this docstring claimed "a
       query that already specifies which sub-variant it wants naturally
       fails condition 1 on its own... no separate text check is needed."
       Real evidence directly disproved this: "فرع شبرا التوفيقية...",
       "هولتر 72 ساعة", and "الماموجرام الديجيتال" all named a specific
       real candidate explicitly, at real retrieval scores of 0.94-0.998,
       and still triggered clarification under the gap check alone. Steps
       3-4 are both domain-agnostic text-overlap checks — no chunk_type
       restriction anywhere in this function, by design (see this
       project's own history: an earlier draft of this fix proposed
       scoping detection to offering-only chunk types; reconsidered and
       dropped before implementation, since it would have permanently
       blocked legitimate branch/package/policy ambiguity detection
       instead of just suppressing the false positives it was aimed at)."""
    top_score = all_results[0]["score"]
    gap_threshold = client_config.whatsapp_variant_ambiguity_score_gap

    seen_names: dict[str, tuple[str, str | None, float]] = {}
    for result in all_results:
        name = _extract_display_name(result["chunk"])
        if name is None:
            continue
        normalized = _normalize_display_text(name)
        if normalized in seen_names:
            continue
        if top_score - result["score"] > gap_threshold:
            continue
        category = _extract_category(result["chunk"])
        seen_names[normalized] = (name, category, result["score"])

    if len(seen_names) < _CLARIFICATION_MIN_DISTINCT_NAMES:
        return None

    candidates = list(seen_names.values())
    grouped = _group_by_category(candidates) or _group_by_name_prefix(candidates)
    if grouped is None:
        return None

    parent_category, suffixes = grouped
    if not _category_overlaps_query(parent_category, resolved_query):
        return None
    if _query_already_specifies_one(resolved_query, suffixes, parent_category):
        return None

    return grouped


# Fixed closing question every _build_clarification_reply output ends
# with, regardless of parent_category/suffixes content — 2026-09-08
# addendum, history-imitation fix. Shared with _is_decline_or_fallback_
# reply below (never duplicated as a second hardcoded literal there) so
# the two can never silently drift apart if this phrasing ever changes.
_CLARIFICATION_REPLY_SUFFIX = "تحب أي نوع فيهم؟"


def _build_clarification_reply(parent_category: str, suffixes: list[str]) -> str:
    """Deterministic, zero-LLM-involvement reply — same reasoning
    _UNAVAILABLE_EVERYWHERE_FALLBACK's own comment already gives for its
    analogous case: every real option here is already fully known,
    structural data (see _detect_variant_ambiguity), so there is nothing
    genuine left for an LLM to phrase a judgment about — only real,
    already-retrieved names assembled into a sentence. Also avoids adding
    a further LLM round-trip to turns already measured running 44-73s in
    real production traffic. Real, disclosed trade-off: the suffixes are
    shown exactly as they appear in the real catalog data (often bilingual
    Arabic/English medical terms, e.g. "Single Arch") rather than a
    polished pure-Arabic translation — inventing a nicer Arabic label per
    suffix would be fabricating business content never actually present in
    the source data (claude.md §3.5), the same discipline this project
    applies everywhere else.

    Always ends with the exact, fixed _CLARIFICATION_REPLY_SUFFIX — this
    is what lets _is_decline_or_fallback_reply recognize and filter this
    exact turn shape out of history on a later turn (see that function's
    own 2026-09-08 comment for the real incident this closes).

    2026-09-09 addendum — real, reported UI readability issues, all fixed
    here rather than by touching the detection logic upstream:
    1. A single comma-joined sentence (the original format) rendered as a
       dense, hard-to-scan paragraph once there were 3+ real options —
       now one option per line, each prefixed with "- ", so the chat UI
       renders a real line-by-line list instead of inline prose.
    2. Each suffix is passed through normalize_bilingual_punctuation
       first — real catalog data occasionally carries malformed
       parenthetical punctuation, an inverted leading colon, or a word
       glued directly onto a paren/opposite script with no separating
       space (see that function's own docstring for the confirmed real
       incidents this closes) that a raw pass-through would have shown
       the patient verbatim.
    3. Any embedded newline within a raw suffix itself (a multi-line
       catalog field value, not this method's own line joining below) is
       collapsed to a single space BEFORE normalization — so "every
       option strictly on one clean line" is a real guarantee of this
       function's OWN "\\n".join(lines) structure below, never something
       a single messy source record could quietly break by contributing
       an extra internal line break of its own.
    Deliberately does NOT try to force every option into a rigid
    "[Arabic] ([English])" template — real suffixes vary in shape (some
    are pure English like "Single Arch", some are Arabic-then-English-
    parenthetical like the EEG durations, and forcing a uniform split
    would mean guessing which part of an arbitrary real string is "the
    Arabic part" for cases that were already well-formed differently,
    risking mangling data that didn't need fixing). Bidi correctness for
    whichever shape a given suffix actually has is handled separately and
    uniformly by helpers.bidi_text.isolate_latin_runs at the API response
    boundary (routes/whatsapp.py), not here."""
    cleaned_suffixes = [
        normalize_bilingual_punctuation(" ".join(suffix.split("\n"))) for suffix in suffixes
    ]
    lines = [f"متاح لدينا أكتر من نوع من {parent_category}، منها:"]
    lines.extend(f"- {suffix}" for suffix in cleaned_suffixes)
    lines.append(_CLARIFICATION_REPLY_SUFFIX)
    return "\n".join(lines)


# Hallucination Lock, Layer 3 (2026-09-08): the one case where Python
# already has COMPLETE, structurally-verified certainty before any
# generation call — both the primary brand-filtered retrieval AND the
# unfiltered cross-brand retrieval independently confirmed no real,
# non-placeholder-shaped answer exists anywhere. There is nothing
# genuine left for an LLM to judge here, so none is asked: this is a
# fixed, non-LLM-generated string, same "not Bucket B, doesn't describe
# a business fact/policy needing verbatim sourcing, it's a generic
# acknowledgment" reasoning GENERATION_UNAVAILABLE_FALLBACK below already
# gives for its own analogous case. Deliberately NOT dynamically
# interpolating the exam/service name into this message: real chunk
# schemas across sheets put that under different, inconsistent column
# names (اسم الفحص / اسم الفرع / اسم التعاقد / ...), and this project's
# own recent audit found the "first populated field" heuristic that
# would be needed to guess which one is itself unreliable per-sheet (see
# the diversity-heuristic finding from the clarifying-question-pathway
# work) — a wrong or malformed interpolated name would be a worse
# failure than a slightly more generic, but always-correct, static one.
_UNAVAILABLE_EVERYWHERE_FALLBACK = (
    "للأسف يا فندم الخدمة دي مش متاحة عندنا حاليًا في أي فرع من فروعنا. "
    "حابب أحولك لحد من الفريق يقدر يفيدك بالتفاصيل؟"
)

# Phrase fragments that reliably signal a PAST assistant turn (as it sits
# in session_state["history"]) was a decline, apology, or generation
# fallback rather than a real grounded answer — 2026-09-02 self-
# reinforcing-decline-loop finding: at temperature=0.0 (fully greedy
# decoding — see _generate_grounded_reply's own temperature history for
# why this is 0.0), a decline sitting in *history* acts as a strong
# imitated precedent on the very next turn even when that next turn's own
# CONTEXT genuinely answers the question, since greedy decoding has no
# randomness to escape the pattern once the model locks onto "last time I
# declined, so I'll decline again." whatsapp_out_of_domain_directive's own
# output is deliberately free-form Arabic (see that directive's own
# docstring/comment), so unlike GENERATION_UNAVAILABLE_FALLBACK and
# REPLY_VERIFICATION_FAILED_FALLBACK below there is no single fixed
# string to match against for that path — this tuple is a heuristic over
# real observed decline/apology phrasing, not an exhaustive contract.
# Extend it, don't rewrite the filtering logic, if a new recurring
# decline phrasing is observed on real traffic.
_DECLINE_PHRASE_MARKERS = (
    "معذرة",        # apology opener shared by both fixed fallbacks above
    "آسف",          # "آسف/آسفة/آسفين" — sorry, any gender/number ending
    "مش واضح",      # e.g. "مش واضحة عندي بالشكل الكافي" — hedged non-answer
    "مش متأكد",     # hedged non-answer / can't confirm
    "مش متخصص",     # out-of-domain directive's own "specialization" framing
    "برا التخصص",   # "outside our specialization" — same directive framing
    "مش بنقدم",     # "we don't offer/provide this"
    "حد من الفريق",  # human-handoff phrasing (REPLY_VERIFICATION_FAILED_FALLBACK
                     # and PENDING_PHASE_REPLY-style turns alike)
)


def _is_decline_or_fallback_reply(content: str) -> bool:
    """True when a stored assistant history turn was a decline, apology,
    or generation fallback rather than a real grounded answer — see
    _DECLINE_PHRASE_MARKERS' own comment for why this can't be a strict
    equality check for every such turn. The two fixed fallback strings
    (generation timeout, verification-gate rejection) are matched exactly
    first since those really are fixed, known strings; the out-of-domain
    decline path's genuinely free-form phrasing then falls to the marker
    heuristic.

    2026-09-08 (CQR-context-loss fix): deliberately does NOT also cover
    a clarification-list reply (_build_clarification_reply) — see
    _is_clarification_reply's own docstring for why that has to be a
    separate, narrower-scoped predicate. This function's own scope is
    unchanged from its original 2026-09-02 design: real decline/apology/
    fallback text only."""
    if content in (GENERATION_UNAVAILABLE_FALLBACK, REPLY_VERIFICATION_FAILED_FALLBACK):
        return True
    return any(marker in content for marker in _DECLINE_PHRASE_MARKERS)


def _is_clarification_reply(content: str) -> bool:
    """True for a deterministic clarification-list reply
    (_build_clarification_reply), matched via its own fixed
    _CLARIFICATION_REPLY_SUFFIX — this project's own deterministic
    output, never a heuristic over free-form text.

    2026-09-08 (CQR-context-loss fix): kept SEPARATE from
    _is_decline_or_fallback_reply, and combined with it only for the
    GENERATION-facing history filter (_is_decline_fallback_or_
    clarification_reply below) — never for IntentRoutingController.
    route_turn's own cqr_history. Real, confirmed incident: an earlier
    version of this fix folded the clarification check directly into
    _is_decline_or_fallback_reply, which BOTH call sites share — that
    silently starved CQR's rewrite_query of the exact turn it needed to
    resolve a terse follow-up ("فك واحد") against, collapsing retrieval
    to noise-level (score 0.0033, down from a correct 0.855) and
    confidently grounding on the wrong exam entirely (CT Mandible 3D
    instead of CBCT Single Arch) — a worse failure than the original
    prose-decline symptom this whole mechanism exists to fix. CQR
    genuinely needs a clarification turn's real content to do its job;
    only generation must avoid imitating its non-JSON shape."""
    return content.endswith(_CLARIFICATION_REPLY_SUFFIX)


def _is_decline_fallback_or_clarification_reply(content: str) -> bool:
    """Combined predicate for the GENERATION-facing history filter ONLY
    (TextReplyController._mode_a_reply/_generate_grounded_reply) — never
    passed to IntentRoutingController.route_turn's cqr_history, which
    uses _filter_decline_history's own default predicate
    (_is_decline_or_fallback_reply alone) instead. See
    _is_clarification_reply's own docstring for the real incident behind
    this split. Generation must not imitate a clarification-list turn's
    own non-JSON shape (the original, separately-confirmed history-
    imitation incident); CQR must still see that turn's real content."""
    return _is_decline_or_fallback_reply(content) or _is_clarification_reply(content)


def _filter_decline_history(history: list, is_excluded=_is_decline_or_fallback_reply) -> list:
    """Drops any assistant turn matched by `is_excluded` from a history
    list, ALONG WITH the user turn immediately preceding it, keeping
    every other turn in original order. Applied to *history* right
    before it's injected into a Mode A generation prompt (see the
    history retrieval in _mode_a_reply) so the model never sees its own
    past decline/fallback/apology as conversational precedent — see
    _DECLINE_PHRASE_MARKERS' own comment for the production incident this
    fixes.

    `is_excluded` (2026-09-08, CQR-context-loss fix) defaults to
    _is_decline_or_fallback_reply — IntentRoutingController.route_turn's
    own cqr_history call relies on this default, unchanged, since CQR
    needs to see a clarification-list turn's real content to resolve a
    terse follow-up against it. TextReplyController's own generation-
    facing history call sites instead pass
    _is_decline_fallback_or_clarification_reply explicitly — see that
    function's own docstring for the real incident this split fixes.

    2026-09-02 (paired-removal fix): a first version of this function
    dropped only the assistant turn via a plain list comprehension,
    leaving the user question that prompted it stranded in the filtered
    result. That orphaned user turn then sat next to whatever real turn
    followed, producing two consecutive `role: user` entries — vLLM's
    chat template enforces strict user/assistant/user/assistant
    alternation, so rendering that history raised `jinja2.exceptions.
    TemplateError: Conversation roles must alternate user/assistant/
    user/assistant/...` the next time this filtered history reached
    generate_reply(). Removing the question along with its declined
    answer keeps the surviving turns correctly alternating, at the cost
    of also losing that one real question from history — an acceptable
    trade-off: a question the model failed to ground is worth less as
    context than an alternation-breaking prompt that crashes the turn
    outright.

    Walks history in original order, appending to (and popping from) a
    single output list, rather than a role-blind list comprehension, so
    each decision can see whatever the previous turn just did. Popping
    is guarded by `filtered and filtered[-1].get("role") == "user"`:
    a well-formed alternating history always has a user turn immediately
    before any assistant turn, but a window slice can legitimately land
    history's very first element on an assistant turn with nothing
    before it in `filtered` yet to pop — that guard makes this a no-op
    drop instead of a crash in that edge case, rather than assuming the
    pop is always safe."""
    filtered: list = []
    for turn in history:
        if turn.get("role") == "assistant" and is_excluded(turn.get("content", "")):
            if filtered and filtered[-1].get("role") == "user":
                filtered.pop()
            continue
        filtered.append(turn)
    return filtered


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
    Mode A: grounded, retrieval-scaled rewrite — always single-chunk,
    unwrapped CONTEXT (see _narrow_context_block; 2026-09-02, single-
    chunk-always change — see _mode_a_reply's own docstring for why the
    earlier multi-chunk broad-breadth path was retired), always in
    Egyptian Arabic, always closing with a dynamic follow-up question. Mode B:
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
        history_window: int,
    ):
        super().__init__()
        self.retrieval_controller = retrieval_controller
        self.client_config_model = client_config_model
        self.generation_client = generation_client
        self.dialogue_state_template_map_model = dialogue_state_template_map_model
        self.reply_verification_controller = reply_verification_controller
        # Same settings.SESSION_HISTORY_WINDOW value SessionStore itself
        # uses (main.py wires both from one source) — needed here too
        # because IntentRoutingController's own [-window:] slice only
        # trims session_state["history"] AFTER this turn's reply is
        # generated, to protect the *next* turn. A session hydrated from
        # Redis/Postgres before a SESSION_HISTORY_WINDOW reduction (or
        # any other reason its stored history is currently oversized)
        # would otherwise still have that full, untrimmed history read
        # and sent to the generation backend for THIS turn — real,
        # observed behavior (a 400 "context length exceeded" recurring
        # on an existing session_id even after lowering the config
        # default) that made the write-time-only slice insufficient.
        self.history_window = history_window
        self.template_parser = TemplateParser()
        self.logger = logging.getLogger(__name__)

    async def reply(
        self, client_id: str, session_id, text: str, resolved_query: str, session_state: dict,
    ) -> tuple[str, str, object | None, str | None, float | None, str | None]:
        """Returns (reply_text, mode, debug_json, outcome_status,
        retrieval_score, topic_override) — mode is "mode_a" or "mode_b",
        surfaced for logging/Postman verification, never used by the
        caller to branch (the decision already happened here).

        outcome_status/retrieval_score/topic_override (2026-09-08,
        Analytics Dashboard pipeline) are None, always, for a Mode B
        turn — verbatim Bucket B/C template substitution is not a RAG
        search attempt, so "answered"/"not_found" has no meaningful
        referent for it; see _mode_a_reply's own docstring for what these
        three values mean on a Mode A turn.

        `resolved_query` (2026-08-30, dual-model architecture) is
        IntentRoutingController's own dedicated query-router sidecar's
        history-aware rewrite of `text` — see QueryRouterInterface's own
        docstring for the full contract. Used only for retrieval inside
        _mode_a_reply; `text` itself is untouched and is what reaches the
        patient-facing PATIENT MESSAGE: block, so a short real reply like
        "اه" still gets a naturally-phrased response, not one that talks
        as if the patient had typed the rewritten query.

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
                return self.template_parser.resolve(bucket, mapping.template_id, **substitutions), "mode_b", None, None, None, None

        reply_text, json_block, outcome_status, retrieval_score, topic_override = await self._mode_a_reply(
            client_id, session_id, text, resolved_query, session_state,
        )
        return reply_text, "mode_a", json_block, outcome_status, retrieval_score, topic_override

    def _mode_b_substitutions(self, session_state: dict) -> dict:
        """Real, sensible values for the placeholders the call-script-derived
        Bucket B templates carry over from their original human-agent source
        (claude.md §3.5) — $brand, $employee_name, etc. Passing every known
        key unconditionally is safe: string.Template.substitute() only
        consumes the ones an individual template actually references and
        ignores the rest, so this doesn't need to know which template it's
        about to render."""
        brand_filter = session_state.get("brand_filter")
        brand_label = _BRAND_LABELS.get(brand_filter, "رايلاب")
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
        broad mode used to. Reverted after a real-data retrieval
        investigation (scripts/investigate_retrieval_task1.py) on the
        resulting regressions: cases with an overwhelmingly dominant,
        unambiguous single correct chunk (score 7.08, repeated in every
        retrieved candidate) still failed to extract once wrapped as
        multiple sources — retrieval clearly wasn't the bottleneck, so
        this had to be the untrained wrapped/multi-chunk format itself.
        One case also showed a concrete, confirmed distractor: a
        topically-adjacent-but-wrong row (different company, different
        number, same field label) that only entered the window because
        top_k_narrow exceeded 1.

        2026-09-02 (single-chunk-always change): the broad-breadth
        wrapped-multi-source path this method used to hand off to on a
        classification of "broad" (via the now-deleted _wrap_sources
        helper) was retired outright — see _mode_a_reply's own docstring.
        This method's own unwrapped, single-chunk CONTEXT shape is now
        unconditionally what every Mode A turn sends the model,
        regardless of breadth classification.

        2026-08-28: each chunk's content is passed through
        _normalize_context_text first — see that helper's own docstring.
        Presentation-layer only, same real content, never touches what
        ChunkingController stored."""
        return "\n\n".join(_normalize_context_text(result["chunk"].content) for result in results)

    async def _generate_grounded_reply(
        self, text: str, history: list, system_prompt: str, context_block: str, max_tokens: int,
        grounding_note: str | None = None,
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
        belongs to the caller, not this helper.

        2026-09-08: no longer takes a `cross_brand_note` param — the
        bolt-on "NOTE — this is cross-brand" instruction this method used
        to append to CONTEXT is retired. Real evidence showed the model
        could ignore that note entirely and answer as a normal same-brand
        confirmation. The Dynamic Cross-Brand Availability Pipeline now
        handles that case with a dedicated, narrowly-scoped `system_prompt`
        (whatsapp_cross_brand_referral_directive) instead — the caller
        (_mode_a_reply) selects which directive to pass in as
        `system_prompt` based on `availability_status`, so this method no
        longer needs to know cross-brand is even a concept.

        `history` is re-filtered here via _filter_decline_history even
        though _mode_a_reply's own history retrieval already filters it
        before the first call — this method is the single place that
        actually builds the `messages` list sent to the model, so this is
        a deliberate defense-in-depth: a future caller/retry path that
        forgets to pre-filter still can't leak a decline/fallback/apology
        turn into the prompt. Filtering an already-filtered list is a
        cheap no-op, not wasted work.

        is_excluded=_is_decline_fallback_or_clarification_reply
        (2026-09-08, CQR-context-loss fix) — this is a GENERATION call
        site, so it must also filter out a clarification-list turn (not
        just a decline), unlike IntentRoutingController.route_turn's own
        cqr_history, which relies on _filter_decline_history's default
        predicate instead. See _is_clarification_reply's own docstring
        for the real incident this split fixes.

        grounding_note (2026-09-09, Option A dynamic specificity fix) —
        see _build_specificity_grounding_note's own docstring for the real
        incident this closes. Appended inside the CONTEXT: section, never
        inside PATIENT MESSAGE: — `text` there must stay exactly what the
        patient literally typed (see this method's own PATIENT MESSAGE:
        comment above), while CONTEXT: is already free-form retrieved
        business text the model reads facts out of, so one more clearly-
        labelled internal line fits its existing shape without touching
        the two-section CONTEXT:/PATIENT MESSAGE: format the fine-tuned
        model was trained on. None on most turns (the common case where
        resolved_query added nothing text didn't already say) — appends
        nothing, so the prompt shape is byte-identical to before this fix
        on every turn that doesn't need it."""
        history = _filter_decline_history(history, is_excluded=_is_decline_fallback_or_clarification_reply)
        context_section = f"CONTEXT:\n{context_block}"
        if grounding_note:
            context_section = f"{context_section}\n\n{grounding_note}"
        user_sections = [context_section, f"PATIENT MESSAGE:\n{text}"]

        messages = [
            {"role": "system", "content": system_prompt},
            *history,
            {"role": "user", "content": "\n\n".join(user_sections)},
        ]
        raw_output = await self.generation_client.generate_reply(messages, temperature=0.0, max_tokens=max_tokens)
        json_block, phrasing = _split_json_and_phrasing(raw_output)
        return json_block, phrasing, raw_output

    @staticmethod
    def _is_placeholder_shaped_chunk(chunk, client_config) -> bool:
        """Dynamic Cross-Brand Availability Pipeline, Interception step
        (2026-09-08) — detects whether a retrieved chunk's own REAL
        content (metadata_payload["field_data"], set by ChunkingController
        independent of anything Mode A later chooses to extract) has the
        STRUCTURAL shape this project's real source data uses to say
        "this doesn't apply here": many real fields, almost all sharing
        the identical value. Deliberately NOT a hardcoded string/phrase
        match against specific business text ("غير متاح ب...", or any
        other literal) — that would only ever catch the exact phrasing
        already observed, never generalize to a different sheet, a
        different brand name, or a differently-worded redirect note using
        the same real structural convention. Counting distinct values vs.
        field count generalizes automatically to any sheet/exam/branch/
        insurance-row using this shape, in any language, with zero
        business-vocabulary knowledge.

        Real incident this fixes: a brand-filtered retrieval for
        "عايز اعمل اشاعة عادية على ضرس" under brand_filter=cairoscan
        returned a single, HIGH-confidence chunk (reranker score 0.7231)
        whose real content was:
            نوع الاشعه: X-Ray ... (real, distinct)
            اسم الفحص: Periapical X-rays ... (real, distinct)
            مسمى الفحص على السيستم: غير متاح بكايروسكان متاح بتكنو سكان
            تحضيرات الفحص: غير متاح بكايروسكان متاح بتكنو سكان
            ... (7 more fields, ALL the identical string)
        10 real fields, only 3 distinct values (ratio 0.3) — yet Mode A's
        own extraction pulled only the two neutral fields (اسم الفحص/نوع
        الاشعه) into json_block, never touched the 8 duplicate-valued
        ones, and confidently told the patient the exam WAS available —
        the opposite of the real, unambiguous ground truth sitting in
        every other field of its own CONTEXT — confirmed on TWO separate
        real test runs, including one where this exact detection fired
        correctly and the fallback swap happened, yet the model STILL
        failed to disclose the cross-brand nature in its phrasing. That
        second failure is why this pipeline now also carries a
        deterministic Hallucination Lock downstream of this detection,
        not just the detection itself.

        Guarded by _PLACEHOLDER_CHECK_MIN_FIELDS: a chunk with too few
        real fields can trivially show a low distinct-ratio by chance,
        unrelated to any real redirect convention — such chunks are never
        flagged by this check at all, regardless of their ratio."""
        field_data = (chunk.metadata_payload or {}).get("field_data") or {}
        field_count = len(field_data)
        if field_count < _PLACEHOLDER_CHECK_MIN_FIELDS:
            return False
        distinct_count = len({str(value).strip() for value in field_data.values()})
        return (distinct_count / field_count) <= client_config.whatsapp_placeholder_chunk_max_distinct_ratio

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
        self, client_id: str, session_id, text: str, resolved_query: str, session_state: dict,
    ) -> tuple[str, object | None, str, float | None, str | None]:
        """Returns (phrasing, json_block, outcome_status, retrieval_score,
        topic_override) — see reply()'s own docstring for what json_block
        is and who consumes it. json_block is always None on the two
        fallback paths below (a timeout or an unusable generation has no
        JSON to report), on the out-of-domain decline path (never part of
        this fine-tuning dataset), and on the clarification-request path
        (a deterministic, non-LLM reply — see _build_clarification_reply).

        topic_override (2026-09-08, Dynamic Disambiguation/Clarification
        Flow) is None on every path except a clarification request, where
        it carries the structurally-derived parent category (e.g. "CBCT
        (3D)") — tasks/log_intent.py uses this directly as extracted_topic
        and skips its own classify_topic call entirely for that turn, since
        this value is both more reliable (grounded in what was actually
        retrieved) and cheaper (zero extra LLM round-trip) than a blind
        guess from the query text alone would be.

        outcome_status/retrieval_score (2026-09-08, Analytics Dashboard
        pipeline) persist, for Phase 6's Intent Log, structural facts this
        method already computes mid-turn and previously discarded once the
        reply text was returned — see migration b7d2e4f61a93's own module
        docstring for the full column-by-column reasoning. outcome_status
        is always exactly one of "not_found" (zero results anywhere, or
        the Dynamic Cross-Brand Availability Pipeline's own
        _AVAILABILITY_UNAVAILABLE_EVERYWHERE — no real answer exists),
        "technical_error" (a real chunk was found and grounded generation
        was attempted, but a timeout or truncation left nothing usable to
        show the patient — an infra failure, not a catalog-coverage gap),
        "escalated_verification" (ReplyVerificationController rejected the
        grounded phrasing), "answered" (a real, verified grounded reply
        — including a successful cross-brand referral, which the pipeline
        treats as an answer, not a gap), or "clarification_requested"
        (2026-09-08, Dynamic Disambiguation/Clarification Flow — real,
        closely-scored competing sub-variants detected in the broad
        candidate set, see _detect_variant_ambiguity). retrieval_score is
        diagnostic-only
        (see the migration docstring for why it does NOT drive
        outcome_status) — the top-1 score of whichever retrieval last
        grounded this turn's decision, or None when nothing was ever
        found.

        `resolved_query` (2026-08-30, dual-model architecture) drives
        every retrieval call in this method — RetrievalController.
        retrieve() has no history of its own, so a short, context-
        dependent reply would otherwise be embedded and searched
        literally. `text` (the patient's real, unmodified message) is
        reserved for the PATIENT MESSAGE: block generation sees, for
        human_handoff_queue, and for ReplyVerificationController — the
        reply's own phrasing and the escalation record both reflect what
        the patient actually typed, never the rewritten query.

        The final step, before returning, is ReplyVerificationController's
        safety gate (Implementation Plan's post-fine-tuning Step 8): the
        phrasing this method is about to return gets checked against its
        own json_block for unverified numeric claims, and swapped for a
        safe fallback (with the real draft escalated to human_handoff_queue)
        if it fails. This runs on every real Mode A answer — never on the
        out-of-domain decline or the two earlier fallback returns below,
        which either have no json_block to verify against or are already
        the safe fallback themselves.

        Single-chunk-always (2026-09-02, multi-turn CQR audit finding —
        superseding the breadth-driven multi-chunk design below): real log
        evidence traced against an actual 4-turn conversation showed
        RetrievalController consistently ranking the correct chunk #1
        even on turns _classify_breadth labeled "broad" — the "broad"
        label was firing because several structurally-identical Branch
        Directory/Insurance Guide records (same field schema, e.g. every
        branch has its own "مواعيد الفروع يوم الجمعة" field) score nearly
        identically for a generic attribute question, not because
        retrieval itself was wrong. Wrapping those look-alike records
        together via the now-deleted _wrap_sources was what actually
        broke the fine-tuned model's own JSON-selection step — it
        returned completely empty {} extraction across all sources even
        with the right fact sitting in source 1, and this reproduced
        regardless of conversation length or CQR (CQR's own resolved_query
        was independently confirmed correct on every turn via the
        [breadth] query= log line, which receives resolved_query — see
        that method's own log line). `results` is therefore now always
        capped to `client_config.whatsapp_retrieval_top_k_narrow`
        (currently 1) regardless of what `breadth` says; `breadth` is
        still computed and logged as a diagnostic signal, but no longer
        decides chunk count, context shape, token budget, or retry
        behavior. Real, disclosed trade-off: whatsapp_mode_a_reply_
        directive's own Rule 5 (a multi-source summary across several
        services) can no longer be satisfied — every reply is now
        single-chunk-grounded even for a query that's genuinely asking
        about several things at once.

        Empty-extraction retry (formerly "widening retry"): if the single
        chunk's own JSON extraction comes back completely empty
        (_is_json_empty) — the model's own signal that it didn't ground
        an answer in what it was shown — this retries generation ONCE
        against that SAME single chunk with an explicit corrective
        instruction (_BROAD_EMPTY_JSON_RETRY_NUDGE) naming the exact
        failure pattern. There is no wider candidate set to escalate to
        anymore (see the single-chunk-always change above) — this never
        asserts an answer exists, it only gives the model one more
        chance at the same real evidence, so it can still decline on the
        retry. The result, retried or not, still goes through
        ReplyVerificationController unchanged."""
        # Sliced here, not trusted from session_state as-is — see the
        # constructor's own comment on why write-time-only truncation
        # (IntentRoutingController) isn't sufficient on its own. Also
        # filtered here (_filter_decline_history) so a past decline/
        # fallback/apology/clarification-list assistant turn is never
        # handed to the LLM as conversational precedent — see that
        # helper's own comment for the self-reinforcing decline loop this
        # closes, and _is_decline_fallback_or_clarification_reply's own
        # docstring for why THIS (generation-facing) call site passes
        # that combined predicate explicitly, unlike IntentRoutingController.
        # route_turn's own cqr_history (which must keep seeing a
        # clarification turn's real content — CQR needs it to resolve a
        # terse follow-up like "فك واحد" against). Filtering AFTER the
        # window slice, not before: the window is sized in real turns of
        # genuine conversation, and a decline turn still occupied a real
        # turn of that budget when it happened.
        history = _filter_decline_history(
            session_state.get("history", [])[-self.history_window:],
            is_excluded=_is_decline_fallback_or_clarification_reply,
        )
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

        # [brand_filter_trace] Step 1 — Initial Brand Filter: the exact
        # session-derived brand_filter value and the metadata_filters
        # dict this turn's retrieval will actually be called with (None
        # here means either no brand was selected, or "brand" isn't in
        # this client's own allowed_metadata_keys — see the `if` above).
        self.logger.info(
            f"[brand_filter_trace] query={text!r} brand_filter={brand_filter!r} "
            f"metadata_filters={metadata_filters!r}"
        )

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
            client_id=client_id, query=resolved_query, metadata_filters=metadata_filters, top_k_override=broad_top_k,
        )

        # Analytics Dashboard pipeline (2026-09-08) — the real, top-1
        # score this turn's primary search produced, captured once here
        # before any later reassignment of all_results (the forced-
        # fallback branch below overwrites all_results with the
        # alternative-brand candidate on a successful referral, and
        # updates this value to match at that same point — see that
        # branch's own comment). None when nothing was found at all.
        retrieval_score = all_results[0]["score"] if all_results else None

        # [brand_filter_trace] Step 2 — First Retrieval Result: whether
        # anything came back at all, how many chunks, and the top
        # chunk's own reranker score (None when all_results is empty).
        self.logger.info(
            f"[brand_filter_trace] first_retrieval: found={bool(all_results)} "
            f"count={len(all_results)} "
            f"top_score={(all_results[0]['score'] if all_results else None)!r}"
        )

        # Dynamic Cross-Brand Availability Pipeline (2026-09-08) —
        # replaces the old cross_brand_note/"not all_results" fallback
        # entirely. See _is_placeholder_shaped_chunk's own docstring and
        # this method's class-level docstring for the full real incident
        # driving this redesign: real evidence showed a brand-filtered
        # chunk can exist, score HIGH, and still structurally represent
        # "not offered under this brand" — a shape a mere "zero results"
        # check could never catch — and that even a correctly-triggered
        # fallback with an explicit bolt-on note failed to make the model
        # disclose the cross-brand nature of its own answer. This
        # produces `availability_status` (_AVAILABILITY_NORMAL /
        # _AVAILABILITY_UNAVAILABLE_HERE / _AVAILABILITY_UNAVAILABLE_
        # EVERYWHERE) and `alternative_brand_label`, both threaded
        # forward into prompt selection and post-generation verification
        # below, so every stage of this turn agrees on the same,
        # deterministically-established ground truth.

        # [brand_filter_trace] Step 3 — Interception: is the brand-
        # filtered top-1 result (if any) real, informative content, or
        # does it structurally look like a "not offered under this
        # brand" redirect row? Logs the real distinct-value ratio that
        # drove the decision, not just the final boolean.
        primary_unavailable = False
        if metadata_filters and not all_results:
            primary_unavailable = True
            self.logger.info(
                "[brand_filter_trace] interception: zero results under active filter -> primary_unavailable=True"
            )
        elif metadata_filters and all_results:
            top_chunk = all_results[0]["chunk"]
            field_data = (top_chunk.metadata_payload or {}).get("field_data") or {}
            field_count = len(field_data)
            distinct_count = len({str(v).strip() for v in field_data.values()}) if field_data else 0
            primary_unavailable = self._is_placeholder_shaped_chunk(top_chunk, client_config)
            self.logger.info(
                f"[brand_filter_trace] interception: field_count={field_count} distinct_count={distinct_count} "
                f"distinct_ratio={(distinct_count / field_count if field_count else None)!r} "
                f"max_distinct_ratio_threshold={client_config.whatsapp_placeholder_chunk_max_distinct_ratio!r} "
                f"min_fields_guard={_PLACEHOLDER_CHECK_MIN_FIELDS} primary_unavailable={primary_unavailable!r}"
            )
        else:
            self.logger.info(
                "[brand_filter_trace] interception: skipped (no brand filter active) -> primary_unavailable=False"
            )

        availability_status = _AVAILABILITY_NORMAL
        alternative_brand_label = None
        if primary_unavailable:
            # [brand_filter_trace] Step 4 — Forced Fallback: re-run
            # retrieval with no brand filter at all, so a genuinely
            # different-brand alternative gets a real chance to surface.
            unfiltered_results = await self.retrieval_controller.retrieve(
                client_id=client_id, query=resolved_query, metadata_filters=None, top_k_override=broad_top_k,
            )
            self.logger.info(
                f"[brand_filter_trace] forced_fallback: found={bool(unfiltered_results)} "
                f"count={len(unfiltered_results) if unfiltered_results else 0} "
                f"top_score={(unfiltered_results[0]['score'] if unfiltered_results else None)!r} "
                f"top_chunk_brand={(unfiltered_results[0]['chunk'].metadata_payload.get('brand') if unfiltered_results else None)!r}"
            )

            # [brand_filter_trace] Step 5 — Correctness Guard & Final
            # Decision (2026-09-08, loop fix — real incident: a query for
            # "رنين مغناطيسي على شرايين البطن" under brand_filter=
            # technoscan had a real, confirmed Cairoscan alternative per
            # this project's own earlier golden-suite data, but the
            # SINGLE-candidate version of this check only ever inspected
            # unfiltered_results[0] — which here happened to be ANOTHER
            # Technoscan-tagged placeholder row scoring even higher than
            # the real alternative, so the correctness guard correctly
            # rejected position 0 and incorrectly gave up entirely,
            # missing a genuine answer sitting at position 1+. Dropping
            # an already-satisfied brand filter can only ever ADD
            # candidates to the pool, never remove the original — a
            # same-brand or still-placeholder-shaped result at position 0
            # says nothing about positions 1..N, so this now scans the
            # FULL unfiltered_results list and commits to the FIRST
            # candidate that is both (a) genuinely a different brand than
            # brand_filter, and (b) not itself placeholder-shaped —
            # exactly the same two per-candidate conditions the old
            # single-check version applied, just no longer stopping after
            # only ever trying position 0.
            found_real_alternative = False
            for index, result in enumerate(unfiltered_results):
                candidate_brand = result["chunk"].metadata_payload.get("brand")
                candidate_is_placeholder = self._is_placeholder_shaped_chunk(result["chunk"], client_config)
                if candidate_brand != brand_filter and not candidate_is_placeholder:
                    all_results = [result]
                    availability_status = _AVAILABILITY_UNAVAILABLE_HERE
                    alternative_brand_label = _BRAND_LABELS.get(candidate_brand, candidate_brand)
                    # Analytics: the referral's own real score now
                    # replaces the (possibly placeholder/None) primary
                    # score captured above — this turn's real grounding
                    # evidence changed, so the diagnostic score follows it.
                    retrieval_score = result["score"]
                    found_real_alternative = True
                    self.logger.info(
                        f"[brand_filter_trace] forced_fallback_scan: valid alternative found at index={index} "
                        f"(of {len(unfiltered_results)} candidates) brand={candidate_brand!r} "
                        f"score={result['score']!r}"
                    )
                    break
                self.logger.info(
                    f"[brand_filter_trace] forced_fallback_scan: index={index} rejected "
                    f"brand={candidate_brand!r} same_brand={candidate_brand == brand_filter!r} "
                    f"is_placeholder_shaped={candidate_is_placeholder!r}"
                )
            if not found_real_alternative:
                availability_status = _AVAILABILITY_UNAVAILABLE_EVERYWHERE
                self.logger.info(
                    f"[brand_filter_trace] forced_fallback_scan: exhausted all "
                    f"{len(unfiltered_results)} candidates, no valid alternative found"
                )
            self.logger.info(
                f"[brand_filter_trace] final_decision: found_real_alternative={found_real_alternative!r} "
                f"availability_status={availability_status!r} alternative_brand_label={alternative_brand_label!r}"
            )
        else:
            self.logger.info(
                f"[brand_filter_trace] final_decision: availability_status={availability_status!r} "
                "(Interception did not fire, no fallback needed)"
            )

        # Still computed and logged — a useful diagnostic signal for a
        # future retrieval-tuning pass (see _classify_breadth's own
        # [breadth] log line) — but no longer used to decide chunk count.
        breadth = self._classify_breadth(resolved_query, all_results, client_config)
        # Always capped to the narrow ceiling now, regardless of breadth
        # (2026-09-02, single-chunk-always change — see this method's own
        # docstring for the multi-turn CQR audit finding behind this: the
        # correct chunk was consistently ranked #1 by RetrievalController
        # even on turns classified "broad", and wrapping several
        # structurally-identical records together was what broke the
        # model's own JSON-selection step, not the ranking itself). Never
        # re-retrieved, just the same real all_results trimmed, so this
        # can't disagree with what was just scored.
        results = all_results[:client_config.whatsapp_retrieval_top_k_narrow]

        # Hallucination Lock, Layer 3 (2026-09-08): the one case where
        # generation is skipped entirely, not just constrained. Both the
        # primary brand-filtered retrieval AND the unfiltered cross-brand
        # retrieval independently, structurally confirmed no real,
        # non-placeholder-shaped answer exists anywhere — there is
        # nothing genuine left for an LLM to judge, so none is asked.
        # Returns before breadth classification, context building, or any
        # generation call — the strongest, most literal way to guarantee
        # zero room for the model to hallucinate an availability claim
        # here: it never gets the chance to make one.
        if availability_status == _AVAILABILITY_UNAVAILABLE_EVERYWHERE:
            self.logger.info(
                f"[brand_filter_trace] Layer 3: availability_status=UNAVAILABLE_EVERYWHERE for {text!r} — "
                "bypassing generation entirely, returning deterministic fallback"
            )
            return _UNAVAILABLE_EVERYWHERE_FALLBACK, None, "not_found", retrieval_score, None

        # Dynamic Disambiguation / Clarification Flow (2026-09-08) — see
        # this method's own class-level constants/helpers
        # (_detect_variant_ambiguity, _build_clarification_reply) for the
        # full mechanism. Checked here, after Layer 3's stronger "nothing
        # available anywhere" signal has already had first claim, and
        # against `all_results` (the real broad-ceiling candidate set,
        # never re-retrieved) — a cross-brand referral has already
        # collapsed `all_results` to a single alternative by this point
        # (see the forced-fallback loop above), so this naturally never
        # fires on that path; an active brand filter with no referral
        # needed still has its own real multi-candidate set intact here.
        # Fully deterministic, zero extra LLM call, zero extra retrieval
        # call — see _build_clarification_reply's own comment for why an
        # LLM phrasing pass isn't used here.
        variant_ambiguity = _detect_variant_ambiguity(all_results, client_config, resolved_query)
        if variant_ambiguity is not None:
            parent_category, suffixes = variant_ambiguity
            self.logger.info(
                f"[clarification] query={text!r} parent_category={parent_category!r} "
                f"suffixes={suffixes!r} — returning clarification request instead of grounding"
            )
            clarification_reply = _build_clarification_reply(parent_category, suffixes)
            return clarification_reply, None, "clarification_requested", retrieval_score, parent_category

        # No relevance-score gate here (tried and removed): real traffic
        # showed it false-positiving on genuinely in-domain narrow
        # queries (exam prep, pricing) whose top score happened to land
        # just under the threshold, even though results[0] was
        # consistently the correct chunk regardless of its numeric score.
        # The retrieved top chunk is trusted unconditionally — the only
        # thing that routes a turn to the out-of-domain path now is
        # retrieval legitimately returning zero chunks.
        #
        # Bound here, not inside the branch below, so it stays a real
        # None (not an undefined name) on the out-of-domain decline path
        # for the verify_and_gate call at the end of this method.
        context_block = None
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
            # in this method: see the empty-extraction retry block below
            # for the full 0.1 -> 0.2 -> 0.15 -> 0.0 story; this
            # out-of-domain call uses the same final value (0.0) for the
            # same reasons.
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
                # not_found, not technical_error: reaching this branch at
                # all already means zero real chunks exist anywhere for
                # this query (see the class docstring's own reasoning) —
                # this timeout only affects whether the decline TEXT
                # itself got generated, not the retrieval-level fact that
                # nothing was found.
                return GENERATION_UNAVAILABLE_FALLBACK, None, "not_found", retrieval_score, None
            json_block, phrasing = _split_json_and_phrasing(raw_output)
        else:
            # Hallucination Lock, Layer 1 (2026-09-08): on a confirmed
            # cross-brand-referral turn (availability_status was already
            # decided deterministically above, well before this point —
            # never an open question left for the model), swap in the
            # dedicated whatsapp_cross_brand_referral_directive instead
            # of the general-purpose whatsapp_mode_a_reply_directive. The
            # two are never combined — this directive's own job is
            # narrower ("phrase this already-settled fact", not "decide
            # what's true"), which is the real, structural difference
            # from the old bolt-on cross_brand_note approach that real
            # evidence showed the model could simply ignore.
            if availability_status == _AVAILABILITY_UNAVAILABLE_HERE:
                system_prompt = self.template_parser.resolve(
                    TemplateBucket.C, "whatsapp_cross_brand_referral_directive",
                    alternative_brand_label=alternative_brand_label,
                )
            else:
                system_prompt = self.template_parser.resolve(TemplateBucket.C, "whatsapp_mode_a_reply_directive")

            # Always the single-chunk, unwrapped shape now (2026-09-02,
            # single-chunk-always change — see this method's own
            # docstring) — `results` itself is already capped to the
            # narrow ceiling above, regardless of `breadth`, so there's no
            # more separate wrapped-multi-source branch to build here.
            context_block = await self._narrow_context_block(results)

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

            # 2026-09-09, Option A dynamic specificity fix — see
            # _build_specificity_grounding_note's own docstring for the
            # real incident this closes (a correctly-retrieved, correctly-
            # scored single chunk still got declined because generation's
            # PATIENT MESSAGE: — deliberately `text`, never `resolved_
            # query` — never itself stated the specific detail CQR used to
            # find that chunk). Computed once here, from the same real
            # `text`/`resolved_query` this turn already has in hand, and
            # threaded into both generation attempts below (initial +
            # empty-JSON retry) so a retry doesn't lose the same grounding
            # the first attempt had.
            grounding_note = _build_specificity_grounding_note(text, resolved_query)
            if grounding_note:
                self.logger.info(f"[grounding_note] query={text!r} resolved_query={resolved_query!r} note appended")

            # Single fixed budget now (2026-09-02, single-chunk-always
            # change) — the earlier 2048 broad ceiling existed
            # specifically for multi-source synthesis replies, which can
            # no longer happen now that context_block is always one
            # chunk. 1024 was raised from 250/100 after the post-fine-tune
            # golden-suite grading (68.7% run) found ~11/67 replies
            # truncated mid-sentence, including 4 where the cutoff landed
            # inside the JSON reasoning block itself and leaked raw,
            # unparseable JSON to the patient — real production
            # CONTEXT/debug_json shapes ran longer than the *training*
            # target-length distribution this budget was originally sized
            # around. The model still stops naturally at
            # NILE_CHAT_STOP_SEQUENCES well before this ceiling on a
            # normal-length reply, so this is a ceiling, not a verbosity
            # change.
            max_tokens = 1024

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
                    text, history, system_prompt, context_block, max_tokens, grounding_note,
                )
            except GenerationTimeoutError as e:
                self.logger.warning(f"Mode A: generation timed out for {text!r} ({e}) — returning fallback reply")
                # technical_error, not not_found: a real, non-placeholder
                # chunk WAS found and grounded generation was attempted —
                # this is an infra timeout, not a catalog-coverage gap.
                return GENERATION_UNAVAILABLE_FALLBACK, None, "technical_error", retrieval_score, None

            # Empty-extraction retry (2026-09-02, formerly "widening
            # retry" — renamed now that there's no wider candidate set
            # left to widen into; see this method's own docstring for the
            # single-chunk-always change this follows from): the
            # generation's own JSON extraction came back completely empty
            # — real evidence that the model didn't reliably select/
            # populate fields even when a real answer exists in the one
            # chunk it was shown. Retries the SAME single-chunk
            # context_block once with an explicit corrective instruction
            # (_BROAD_EMPTY_JSON_RETRY_NUDGE) naming the exact failure
            # pattern observed on real traffic — a source returning {}
            # fields while the phrasing still correctly quoted a real
            # fact, which then failed ReplyVerificationController's
            # numeric-grounding check for no real reason (see that
            # controller's own comment on why context_block is now also
            # passed to it, addressing the same root symptom from the
            # other side). No breadth branching anymore — this is now the
            # only retry path, regardless of what `breadth` said.
            if _is_json_empty(json_block):
                self.logger.info(
                    f"[empty_json_retry] extraction empty for {text!r}, retrying with corrective instruction"
                )
                try:
                    retry_system_prompt = "\n".join([system_prompt, _BROAD_EMPTY_JSON_RETRY_NUDGE])
                    json_block, phrasing, raw_output = await self._generate_grounded_reply(
                        text, history, retry_system_prompt, context_block, max_tokens, grounding_note,
                    )
                except GenerationTimeoutError as e:
                    # Best-effort only — if the retry itself times out,
                    # silently keep the original (empty-JSON) attempt's
                    # result rather than failing the whole turn; it still
                    # flows into the exact same downstream empty-phrasing-
                    # fallback / ReplyVerificationController path it would
                    # have without this retry existing.
                    self.logger.warning(
                        f"[empty_json_retry] retry generation timed out for {text!r} ({e}) — keeping original attempt"
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
            # technical_error — same reasoning as the grounded-branch
            # timeout return above: a real chunk was found, generation
            # ran, only the truncated output itself is unusable.
            return GENERATION_UNAVAILABLE_FALLBACK, None, "technical_error", retrieval_score, None

        # Unlike the truncation-fallback branch just above, json_block here
        # is a real, successfully-extracted artifact of what the model
        # actually computed — it's the reason a rejection would even be
        # detectable. So it's still returned as debug_json even if the
        # gate below swaps out the phrasing: a human reviewing
        # human_handoff_queue (or scripts/collect_golden_responses.py)
        # needs exactly this JSON to see what the model got right in
        # extraction but phrased ungrounded, which is a different, more
        # specific failure than "no real answer was produced at all".
        # context_block stays None on the zero-chunk out-of-domain decline
        # path (see its own initialization above) — that path has no
        # json_block to check against either, so verify_and_gate returns
        # early on json_block is None regardless.
        #
        # Hallucination Lock, Layer 2 (2026-09-08): required_phrase is
        # the real alternative brand's own Arabic label ONLY on a
        # confirmed cross-brand-referral turn, None otherwise (every
        # other call shape is completely unaffected — see verify_and_
        # gate's own docstring for the full reasoning). Defense-in-depth
        # against Layer 1's directive being ignored exactly like the old
        # bolt-on cross_brand_note was: independently verifies the real
        # alternative brand name actually made it into the model's own
        # phrasing before it ever reaches the patient.
        verified_reply = await self.reply_verification_controller.verify_and_gate(
            client_id, session_id, text, phrasing, json_block, context_block,
            required_phrase=alternative_brand_label,
        )

        # Analytics: reaching this shared tail with `results` empty only
        # ever happens via the zero-chunk/out-of-domain branch above (see
        # that branch's own "if not results:" condition) — not_found,
        # regardless of how the decline text itself came out. With
        # `results` non-empty, a real chunk grounded this reply; the only
        # remaining question is whether ReplyVerificationController
        # trusted the model's own phrasing enough to let it through.
        # Comparing against the exact same REPLY_VERIFICATION_FAILED_
        # FALLBACK sentinel _is_decline_or_fallback_reply already matches
        # exactly elsewhere in this file — not new string-matching against
        # the reply's own free-form business content.
        if not results:
            outcome_status = "not_found"
        elif verified_reply == REPLY_VERIFICATION_FAILED_FALLBACK:
            outcome_status = "escalated_verification"
        else:
            outcome_status = "answered"

        return verified_reply, json_block, outcome_status, retrieval_score, None
