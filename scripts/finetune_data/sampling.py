"""The four data-generation passes (Stages 1-4 of the data-prep plan),
sourced from real knowledge_chunks via ChunkModel — never raw SQL, never
a fabricated/synthetic chunk (claude.md §4.3's real-data discipline
applies to training data exactly as it applies to every other
verification pass this session built).

Every pass is structured in three phases: (A) build every candidate
purely from local data (chunks, retrieval results) with NO teacher call
involved yet, (B) hand the whole candidate list to
teacher.execute_requests() in one shot — which batches them through the
Message Batches API for a full run (50% off standard pricing; real
measured cost on the old one-call-at-a-time path was exactly double this,
matching the standard-vs-batch rate ratio) or runs them one at a time
live for a `--limit` smoke test, (C) walk the results and apply the same
truncation-check/parse/write logic regardless of which path executed
them. Nothing in a pass depends on a PRIOR teacher response — sampling
decisions (which chunk/label/sheet to use) are entirely deterministic
Python, computed up front — which is exactly what makes whole passes
batchable instead of only single calls.

Each pass writes its own raw_*.jsonl file (one record per line) via the
shared `_write_record` helper. Every record carries a `custom_id` (see
teacher.stable_id) and a `grounding` block holding the REAL structured
field_data used to build its `input` text — never re-derived later by
re-parsing the flattened `[Document: ...] label: value. label2: value2...`
string that ChunkingController produces (claude.md §3.8's join format is
period-separated and prefix-annotated, which is fragile to reverse-parse;
this session already hit exactly that bug once, on a differently-shaped
parsing mistake). Since sampling.py builds every context string from a
`ChunkRecord.field_data` dict it already has in hand, grounding_gate.py
(Stage 5) never needs to reconstruct that dict from text at all.

Resumability (checkpoint.py): every pass first filters its candidate set
against whatever custom_ids are already durably written to its own
raw_<pass>.jsonl, and reconnects to any batch left in-flight by a prior
crash/disconnect before submitting anything new — see
_run_checkpointed_batch. A resumed run pays for exactly the work it
hasn't already paid for, never less, never twice.
"""

import json
import random
from dataclasses import dataclass

from stores.llm.templates.template_parser import TemplateParser, TemplateBucket

from . import checkpoint, teacher

FIXED_SEED = 101  # same seed llm_finetuning.ipynb uses (random.Random(101).shuffle)
NARROW_VARIATIONS_PER_LABEL = 3  # anti-memorization: distinct real chunk instances per field label
COMPOUND_FRACTION = 0.4  # 2026-08-24 redesign: share of narrow_positive candidates trained as
# multi-field (2-3 key) extraction from the FULL chunk, now that FieldSelectionController is being
# removed from production — the model must learn to decide for itself how many fields a question
# needs, not just extract one pre-selected field. The other 60% stay single-field so that
# already-proven behavior isn't diluted. Applied per-candidate via a dedicated seeded RNG so the
# single/compound assignment is reproducible across resumed runs (see run_pass1_narrow_positive).
BROAD_PROBES_PER_SHEET = 4  # bumped 2->4 to address narrow/broad category imbalance — doubles broad_positive's target (~28 -> ~56) without touching narrow_positive
MIN_LABEL_OCCURRENCES_FOR_GENERICNESS_CHECK = 20  # below this, too few real samples to judge a
# label's genericness either way — keep it eligible by default rather than risk excluding a rare,
# genuinely specific field on small-sample noise.
CROSS_REFERRAL_DISCLAIMER_THRESHOLD = 0.6  # 2026-08-25: real measurement (docker exec psql) against
# raylab's actual brand-tagged chunks — 181/1094 real chunks have >=60% of their OWN field_data
# values byte-identical to each other, the structural signature of a single blanket note (real,
# manually-confirmed via sampling: a cross-brand availability disclaimer — "not available under
# this brand, available under the sibling brand" — repeated across most of a row's fields)
# dominating that row. Detected dynamically, per chunk, never keyed on brand-specific text or a
# hardcoded sheet name — this generalizes to Examinations, Sheet1, Branch Directory, Weights,
# Anesthesia, or any future sheet/disclaimer pattern the same way.
MAX_CROSS_REFERRAL_SEEDS_PER_BRAND_SHEET = 3  # capped like NARROW_VARIATIONS_PER_LABEL — per
# (brand, sheet_name) group, so a sheet with many disclaimer-dominated rows (Examinations has the
# most) doesn't crowd out representation of the others.
MAX_GENERIC_LABEL_DISTINCT_RATIO = 0.08  # 2026-08-25: real measurement against raylab's actual
# field_data (docker exec psql, not guessed) — distinct(value)/count(*) per label. Operational
# boilerplate labels cluster well below this (Acoount/account variants 0.5-3%, نوع الاشعه 2.4%,
# تعليمات الحضور — the exact field whose real invented questions were observed retrieving a CBCT
# chunk, an H. Pylori stool test, and a Branch Directory entry for a Cervical Spine seed, all
# correctly caught as safe absence examples by Phase 2/1b but at real, avoidable teacher-call
# cost — at 6.3%), while labels with genuine per-row variation sit comfortably above it (موعد
# استلام التقرير 10.5%, package/pricing fields 12%+). See run_pass1_narrow_positive's diversity
# sampling for where this excludes a label from SEEDING ONLY — never from a chunk's real content,
# retrieval, or what the model can be asked about in production (unrelated to §3.8's own boilerplate-
# exclusion reversal, which was about not hiding real content from retrieval; this never does that).
ABSENCE_COUNT = 60
AMBIGUOUS_DIRECT_COUNT = 30
AMBIGUOUS_CROSS_CHUNK_COUNT = 20

# A small, generic, dynamically-parameterized set of broad-question
# shapes — NOT per-sheet hardcoded content (sheet_name is the only
# variable, read live from the chunk's own metadata, claude.md §3.6).
# Same category of allowed exception as GENERATION_UNAVAILABLE_FALLBACK
# in TextReplyController: a small operational string, not business
# content, that happens to be Python-literal rather than teacher-
# generated — avoids doubling the teacher-call count just to invent a
# probe query shape a fixed template already covers just as well.
BROAD_PROBE_QUESTION_TEMPLATES = [
    "عندكم إيه من {sheet_name}؟",
    "ممكن تقوليلي كل حاجة عندكم في {sheet_name}؟",
    "إيه كل الخيارات المتاحة عندكم في {sheet_name}؟",
    "ممكن أعرف كل التفاصيل الخاصة بـ {sheet_name} عندكم؟",
]

TEMPLATE_PARSER = TemplateParser()


@dataclass
class ChunkRecord:
    chunk_id: object
    content: str
    field_data: dict
    sheet_name: str | None
    source_file: str | None
    brand: str | None  # ChunkingController's own promoted top-level metadata key (§3.5 brand-value-
    # aliases), never re-derived here — the same real value RetrievalController's metadata_filters
    # would match on. None for the ~48% of chunks with no brand-bearing field at all.


def _base_system_prompt() -> str:
    base_directive = TEMPLATE_PARSER.resolve(TemplateBucket.C, "whatsapp_mode_a_reply_directive")
    return teacher.build_teacher_system_prompt(base_directive)


def _write_record(out_path, record: dict) -> None:
    with open(out_path, "a", encoding="utf-8") as dest:
        dest.write(json.dumps(record, ensure_ascii=False) + "\n")


def _load_jsonl(path) -> list[dict]:
    if not path.exists():
        return []
    records = []
    with open(path, encoding="utf-8") as source:
        for line in source:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def _load_resolved_source_ids(paths) -> set[str]:
    """Every narrow_hypothesis custom_id whose real-retrieval
    classification (Pass 1's Phase 2/1b, see run_pass1_narrow_positive)
    has already been durably finalized in a prior run — via a direct
    accept/reattribution (written to out_path with
    source_hypothesis_custom_id == its own custom_id), a correction-batch
    result (out_path or absence_out_path, both stamp
    source_hypothesis_custom_id), or a zero-retrieval discard
    (discarded_path). Checked before Phase 2 re-runs real retrieval for a
    hypothesis, so a resumed run doesn't re-query already-resolved ones."""
    resolved: set[str] = set()
    for path in paths:
        for record in _load_jsonl(path):
            source_id = record.get("source_hypothesis_custom_id") or record.get("custom_id")
            if source_id:
                resolved.add(source_id)
    return resolved


def _field_data_matches(candidate_json, field_data: dict) -> bool:
    """True when every key/value in candidate_json is present, byte-exact,
    in field_data. The same deterministic equality grounding_gate.py's
    _check_keys_and_values uses, duplicated here rather than imported —
    this is a Stage-1-time local classification helper (does a real
    near-duplicate chunk still support this hypothesis's answer?), not a
    Stage-5 QA gate, and Stage 5 documents itself as deliberately
    dependency-free; the two stay independent on purpose."""
    if not isinstance(candidate_json, dict) or not candidate_json:
        return False
    for key, value in candidate_json.items():
        if key not in field_data or str(field_data[key]).strip() != str(value).strip():
            return False
    return True


def _detect_cross_referral_seeds(
    working_set: list[ChunkRecord], *, threshold: float, max_per_brand_sheet: int,
) -> list[ChunkRecord]:
    """Real, dynamic detection of "disclaimer-dominated" chunks — rows
    where a high fraction of the chunk's OWN field_data values are
    byte-identical to each other, regardless of what the shared text
    actually says or which sheet the row is on (see
    CROSS_REFERRAL_DISCLAIMER_THRESHOLD's own real-measurement note).
    These are guaranteed inclusion as Pass 1 seeds, capped per (brand,
    sheet_name) group — by-label diversity sampling usually wouldn't pick
    them (a single field label spread across hundreds of chunks rarely
    lands on the one disclaimer-heavy row by chance), and leaving them to
    chance is exactly what silently produced zero training coverage of
    real cross-brand-referral behavior before this fix. Chunks with no
    brand at all are skipped — this scenario is specifically about
    brand-restricted retrieval, which only applies to brand-tagged rows."""
    grouped: dict[tuple, list[ChunkRecord]] = {}
    for record in working_set:
        if not record.brand or not record.field_data:
            continue
        values = [str(v).strip() for v in record.field_data.values()]
        mode_count = max(values.count(v) for v in set(values))
        if mode_count / len(values) >= threshold:
            grouped.setdefault((record.brand, record.sheet_name), []).append(record)

    selected: list[ChunkRecord] = []
    for group_records in grouped.values():
        selected.extend(group_records[:max_per_brand_sheet])
    return selected


def narrow_intermediate_paths(out_path) -> dict:
    """The three bookkeeping files run_pass1_narrow_positive's Phase
    1a/2/1b write to, derived from out_path's own directory. Exposed as
    its own function (not just inlined in run_pass1_narrow_positive) so
    the orchestration script's --fresh wipe can target the exact same
    paths — these must never drift apart, or --fresh silently leaves
    stale intermediate state behind (the real 2026-08-25 bug this fixes:
    a --fresh run that wipes out_path but not raw_narrow_correction.jsonl
    causes already-billed corrections to be skipped by the "already done"
    check without their final write ever landing back in out_path)."""
    return {
        "narrow_hypothesis": out_path.parent / "raw_narrow_hypothesis.jsonl",
        "narrow_correction": out_path.parent / "raw_narrow_correction.jsonl",
        "narrow_retrieval_discarded": out_path.parent / "raw_narrow_retrieval_discarded.jsonl",
    }


def _write_final_narrow(out_path, hyp: dict, *, content: str, field_data: dict) -> None:
    """Writes a hypothesis record as final narrow_positive output,
    unmodified except for which real chunk it's now grounded in — used by
    both the "confirmed" and "reattributed" branches of Phase 2, whose
    only difference is which chunk's content/field_data gets passed in."""
    _write_record(out_path, {
        "custom_id": hyp["custom_id"],
        "pass": "narrow_positive",
        "source_hypothesis_custom_id": hyp["custom_id"],
        "instruction": hyp["instruction"],
        "input": content,
        "output_json": hyp["output_json"],
        "output_phrasing": hyp["output_phrasing"],
        "raw_output": hyp["raw_output"],
        "grounding": {"type": "narrow", "field_data": field_data},
    })


def _use_batch(limit: int | None, force_batch: bool) -> bool:
    """Full run (no limit) batches for the 50% discount; a --limit smoke
    test runs live by default so a couple of rows don't sit in batch
    scheduling. `force_batch` overrides that for a --limit run
    specifically to exercise the real Batches API (submit/poll/collect)
    cheaply on a couple of rows before trusting it on a full, expensive
    run — the smoke test alone never touches this code path otherwise."""
    return force_batch or limit is None


async def _run_checkpointed_batch(
    client, *, pass_label: str, out_path, state_path, new_meta: dict[str, dict],
    build_item, write_fn, use_batch: bool,
) -> list:
    """Shared resume-then-submit flow every pass's teacher-call phase
    goes through. Two ordered phases, each following the identical
    collect-results -> write-every-record -> ONLY-THEN-clear-state
    sequence — clearing checkpoint state before every one of a batch's
    results is durably on disk would let a crash mid-write cause the next
    run to treat those exact items as brand new and pay for them twice:

    1. Resume: if batch_state.json has an in-flight batch recorded for
       this pass (left over from a prior crash/disconnect), reconnect to
       it via resume_batch_id — never resubmit those candidates — using
       the items_meta persisted alongside the batch_id at submit time
       (there is no other way to know what each of that batch's
       custom_ids corresponds to). Anything among its results already
       durably written (a crash mid-write-loop last time, not mid-poll)
       is skipped rather than double-written.
    2. Submit: whatever of `new_meta` isn't already durably written
       (callers have already filtered this against
       checkpoint.load_done_custom_ids before calling) goes out as one
       fresh batch (or fresh live calls, off the full-run path).

    build_item(custom_id, meta) -> teacher.BatchRequestItem builds a NEW
    candidate's request; never called for a resumed batch's items, since
    those were already built and billed in a prior run.
    write_fn(custom_id, meta, result: TeacherCallResult | None) -> Any |
    None cost-tracks, truncation-checks, parses, and _write_record()s one
    item, returning a per-pass "contribution" (e.g. the generated
    question) or None if skipped/rejected. Returns every non-None
    contribution from both phases, in the order processed."""
    contributions = []

    resume_entry = checkpoint.load_batch_state(state_path).get(pass_label)
    if resume_entry is not None:
        resumed_meta = resume_entry["items_meta"]
        results = await teacher.execute_requests(
            client, [], use_batch=use_batch, pass_label=pass_label,
            resume_batch_id=resume_entry["batch_id"],
        )
        done_ids = checkpoint.load_done_custom_ids(out_path)
        for custom_id, meta in resumed_meta.items():
            if custom_id in done_ids:
                continue  # already made it to disk before whatever interrupted this batch last time
            contribution = write_fn(custom_id, meta, results.get(custom_id))
            if contribution is not None:
                contributions.append(contribution)
        checkpoint.clear_batch_state(state_path, pass_label)

        # `new_meta` was computed by the CALLER against done_ids read
        # BEFORE this resume phase ran. Both the resumed batch and
        # new_meta were independently derived from "whatever isn't done
        # yet against the same target list" — so it's the COMMON case,
        # not an edge case, for the resume phase above to have just
        # satisfied exactly what new_meta still thinks is missing.
        # Re-check against what's ACTUALLY on disk now, or the next block
        # pays for and re-submits a candidate a few lines above just paid
        # for — this is the literal bug a real interrupted run surfaced.
        done_ids = checkpoint.load_done_custom_ids(out_path)
        new_meta = {cid: meta for cid, meta in new_meta.items() if cid not in done_ids}

    if new_meta:
        pending = [build_item(custom_id, meta) for custom_id, meta in new_meta.items()]
        results = await teacher.execute_requests(
            client, pending, use_batch=use_batch, pass_label=pass_label,
            on_batch_submitted=lambda batch_id: checkpoint.record_batch_submitted(
                state_path, pass_label, batch_id, new_meta,
            ),
        )
        for custom_id, meta in new_meta.items():
            contribution = write_fn(custom_id, meta, results.get(custom_id))
            if contribution is not None:
                contributions.append(contribution)
        checkpoint.clear_batch_state(state_path, pass_label)

    return contributions


# ---------------------------------------------------------------------
# Stage 0
# ---------------------------------------------------------------------

async def load_working_set(chunk_model, client_id: str) -> list[ChunkRecord]:
    """Every chunk for this client that has structured field_data — every
    chunk synced under the current ChunkingController does (§3.8), so
    this only ever excludes a genuinely stale pre-field_data chunk, if
    one somehow still existed. Fixed-seed shuffle, matching the
    notebook's own raw_data load-and-shuffle step (cell-29)."""
    chunks = await chunk_model.get_all_chunks(client_id)
    working_set = [
        ChunkRecord(
            chunk_id=chunk.id,
            content=chunk.content,
            field_data=dict((chunk.metadata_payload or {}).get("field_data") or {}),
            sheet_name=(chunk.metadata_payload or {}).get("sheet_name"),
            source_file=chunk.source_file,
            brand=(chunk.metadata_payload or {}).get("brand"),
        )
        for chunk in chunks
        if (chunk.metadata_payload or {}).get("field_data")
    ]
    # get_all_chunks has no ORDER BY — row order isn't guaranteed stable
    # across separate process invocations. Sort by the real chunk_id
    # BEFORE the seeded shuffle so the shuffle's input (and therefore its
    # output) is identical every run — resumability across process
    # restarts depends on candidate order being genuinely reproducible,
    # not just "shuffled with the same seed" applied to a different start.
    working_set.sort(key=lambda record: str(record.chunk_id))
    random.Random(FIXED_SEED).shuffle(working_set)
    return working_set


# ---------------------------------------------------------------------
# Pass 1 — Narrow Positive
# ---------------------------------------------------------------------

async def run_pass1_narrow_positive(
    *, client, working_set: list[ChunkRecord], retrieval_controller, client_config, client_id: str,
    cost_tracker, out_path, absence_out_path, state_path,
    limit: int | None = None, force_batch: bool = False,
) -> list[str]:
    """2026-08-25 redesign (Part A of the retrieval-fidelity plan): a
    hypothesis built from a sampled chunk is never trusted as-is — it's
    verified against what REAL retrieval would actually hand production
    for that exact question, and corrected when it wouldn't. Three
    phases, the first and third each their own Batches-API submission (50%
    discount preserved — the phase in between is local, non-Claude, so it
    doesn't break batching):

    Phase 1a (batched) — same diversity sampling as before: walk the
    working set grouped by field label, take up to
    NARROW_VARIATIONS_PER_LABEL distinct real chunk instances per label
    (never the same chunk repeated) so the candidate pool spans many real
    chunks. This is a DIVERSITY mechanism only — the sampled label doesn't
    determine the ground-truth key. For each sampled "seed" chunk, the
    teacher sees its full real `content` and every real field_data key as
    JSON KEYS ALLOWED, and invents a question needing 1 field (60%) or 2-3
    co-occurring fields (40%, COMPOUND_FRACTION), then answers it. This is
    a HYPOTHESIS only — (question, output_json, phrasing) provisionally
    grounded in the seed chunk — written to its own raw_narrow_hypothesis
    .jsonl, not yet to out_path.

    Phase 2 (local, zero Claude cost) — once Phase 1a's batch fully
    completes, for every hypothesis not already resolved by a prior run
    (_load_resolved_source_ids), run the question through the REAL
    RetrievalController.retrieve — the identical call TextReplyController
    .reply makes, at the same broad-ceiling-first sequence production
    uses — and classify by the real top result:
      - same chunk_id as the seed → confirmed; write directly to out_path
        (the common case, since the question was generated from that
        chunk's own text).
      - different chunk_id, but its real field_data still supports the
        hypothesis's exact output_json (_field_data_matches) → a genuine
        real near-duplicate (this data has real branch-level repeats);
        re-point grounding at the actually-retrieved chunk and write to
        out_path unchanged otherwise.
      - different chunk_id, real field_data does NOT support it → the
        actual "wrong chunk" case; flagged for Phase 1b, never discarded.
      - zero retrieval results → logged to raw_narrow_retrieval_discarded
        .jsonl. Production routes empty retrieval to the out-of-domain
        decline path, which never touches JSON extraction — outside this
        dataset's scope.

    Phase 1b (batched, only the flagged subset — usually a minority, since
    a question generated FROM a chunk should usually retrieve that same
    chunk) — reuses the EXISTING build_verification_prompt /
    ANSWER_GIVEN_QUESTION_TASK mechanism Pass 3 (Absence) already uses:
    given the real question and the REAL retrieved chunk's real content,
    the teacher determines the correctly-grounded answer for what
    retrieval actually returned. Non-empty output_json → written to
    out_path as a real narrow_positive example, discovered via retrieval
    rather than by construction. Empty output_json → written to
    absence_out_path — a real, discovered retrieval-mismatch absence
    example, complementing (not replacing) Pass 3's synthetic ones.

    Net effect: every record that ends up in out_path is grounded in
    exactly the chunk real retrieval would hand production for that exact
    question — never an assumed pairing. Returns every instruction in
    out_path (prior runs' plus this run's), for Pass 3/4 to sample
    mismatched pairings from."""
    intermediate_paths = narrow_intermediate_paths(out_path)
    hypothesis_path = intermediate_paths["narrow_hypothesis"]
    correction_path = intermediate_paths["narrow_correction"]
    discarded_path = intermediate_paths["narrow_retrieval_discarded"]

    # ---- Phase 1a: hypothesis batch ----
    by_label: dict[str, list[ChunkRecord]] = {}
    for record in working_set:
        for label in record.field_data:
            by_label.setdefault(label, []).append(record)

    # Exclude near-universally-duplicated "boilerplate" labels from
    # DIVERSITY SEEDING only (MAX_GENERIC_LABEL_DISTINCT_RATIO) — never
    # from a chunk's real content, retrieval, or what the model can be
    # asked about in production. A question invented purely from such a
    # field carries no chunk-specific signal, so real retrieval can't
    # reliably return the seed chunk back — it was landing on an
    # essentially arbitrary sibling instead, wasting the full
    # hypothesize+retrieve+correct cost on what always resolves to an
    # absence example Pass 3 already produces directly, far more cheaply.
    excluded_labels: list[str] = []
    for label, records in list(by_label.items()):
        if len(records) < MIN_LABEL_OCCURRENCES_FOR_GENERICNESS_CHECK:
            continue
        distinct_values = {str(record.field_data[label]).strip() for record in records}
        if len(distinct_values) / len(records) <= MAX_GENERIC_LABEL_DISTINCT_RATIO:
            excluded_labels.append(label)
            del by_label[label]

    if excluded_labels:
        print(
            f"    [narrow_diversity] excluded {len(excluded_labels)} near-universal boilerplate "
            f"label(s) from seed diversity (distinct-value ratio <= {MAX_GENERIC_LABEL_DISTINCT_RATIO}): "
            f"{excluded_labels}"
        )

    # Guaranteed cross-referral seeding (2026-08-25) — real, dynamic
    # detection (_detect_cross_referral_seeds), not left to chance via
    # by-label sampling, and independent of the boilerplate-label
    # exclusion above (a disclaimer-dominated row's dominant field is
    # sometimes itself an excluded generic label, e.g. تعليمات الحضور —
    # exclusion only stops it being picked BY THAT LABEL, never stops it
    # being seeded here). "__cross_referral__" is a pseudo-label, never a
    # real field name, so it can't collide with by-label custom_ids and
    # every downstream mechanism (compound-assignment, dedup, mode-2
    # brand-filtered classification below) treats these uniformly with
    # everything else. Placed FIRST in `candidates`, ahead of the
    # (usually much larger) by-label list — a `--limit N` smoke test
    # truncates from the front, so this is what makes a focused, cheap
    # smoke test of this exact feature possible at all; otherwise a small
    # limit could truncate away every cross-referral seed before it's
    # ever reached.
    cross_referral_seeds = _detect_cross_referral_seeds(
        working_set, threshold=CROSS_REFERRAL_DISCLAIMER_THRESHOLD,
        max_per_brand_sheet=MAX_CROSS_REFERRAL_SEEDS_PER_BRAND_SHEET,
    )
    if cross_referral_seeds:
        groups = {(record.brand, record.sheet_name) for record in cross_referral_seeds}
        print(
            f"    [narrow_diversity] added {len(cross_referral_seeds)} guaranteed cross-referral "
            f"seed(s) across {len(groups)} (brand, sheet) group(s): {sorted(groups)}"
        )

    candidates: list[tuple[str, ChunkRecord]] = [("__cross_referral__", record) for record in cross_referral_seeds]
    candidates.extend(
        (label, record)
        for label, records in by_label.items()
        for record in records[:NARROW_VARIATIONS_PER_LABEL]
    )
    if limit is not None:
        candidates = candidates[:limit]

    # Deterministic single/compound assignment, walked in the same fixed
    # candidate order every run (working_set is already seeded-shuffled —
    # see load_working_set — so this order is itself reproducible) so a
    # resumed run reassigns the same mode to the same candidate rather
    # than drawing a fresh coin flip each time. Cross-referral seeds never
    # draw from mode_rng at all (kept single-fact, matching the real
    # "is X available under my brand" question shape this scenario
    # actually has) — despite now sitting first in `candidates`, that
    # skip means they never consume a draw regardless of position, so
    # by-label candidates' own mode assignments are unaffected either way.
    mode_rng = random.Random(FIXED_SEED + 4)

    hyp_done_ids = checkpoint.load_done_custom_ids(hypothesis_path)
    hyp_new_meta: dict[str, dict] = {}
    for label, record in candidates:
        if label == "__cross_referral__":
            is_compound = False
        else:
            is_compound = mode_rng.random() < COMPOUND_FRACTION and len(record.field_data) >= 2
        mode = "compound" if is_compound else "single"
        custom_id = teacher.stable_id("narrow", str(record.chunk_id), label, mode)
        if custom_id in hyp_done_ids:
            continue
        hyp_new_meta[custom_id] = {
            "context_text": record.content,
            "field_data": dict(record.field_data),
            "allowed_keys": list(record.field_data.keys()),
            "compound": is_compound,
            "seed_chunk_id": str(record.chunk_id),
            "seed_brand": record.brand,
        }

    already_done = len(candidates) - len(hyp_new_meta)
    if already_done:
        print(f"    [narrow_hypothesis] {already_done}/{len(candidates)} already done — resuming, {len(hyp_new_meta)} new this run")

    system_prompt = _base_system_prompt()

    def build_hypothesis_item(custom_id: str, meta: dict) -> teacher.BatchRequestItem:
        return teacher.BatchRequestItem(
            custom_id=custom_id, system_prompt=system_prompt,
            user_message=teacher.build_generation_prompt(
                context_text=meta["context_text"],
                allowed_keys_description=json.dumps(meta["allowed_keys"], ensure_ascii=False),
                compound=meta["compound"],
            ),
        )

    def write_hypothesis(custom_id: str, meta: dict, result: teacher.TeacherCallResult | None):
        if result is None:
            return None  # errored/canceled/expired batch entry — nothing billed, nothing to parse
        cost_tracker.record("narrow_positive", result.input_tokens, result.output_tokens)
        if result.truncated():
            return None  # max_tokens cut it off — parsing might still "succeed" on a partial JSON; never trust it

        question, json_block, phrasing = teacher.extract_question_json_and_phrasing(result.raw_text)
        if question is None or json_block is None or not isinstance(json_block, dict):
            return None

        _write_record(hypothesis_path, {
            "custom_id": custom_id,
            "pass": "narrow_hypothesis",
            "instruction": question,
            "input": meta["context_text"],
            "output_json": json_block,
            "output_phrasing": phrasing,
            "raw_output": result.raw_text,
            "seed_chunk_id": meta["seed_chunk_id"],
            "seed_brand": meta["seed_brand"],
        })
        return question

    if candidates:
        await _run_checkpointed_batch(
            client, pass_label="narrow_hypothesis", out_path=hypothesis_path, state_path=state_path,
            new_meta=hyp_new_meta, build_item=build_hypothesis_item, write_fn=write_hypothesis,
            use_batch=_use_batch(limit, force_batch),
        )

    # ---- Phase 2: local, real-retrieval classification (zero Claude cost) ----
    hypotheses = _load_jsonl(hypothesis_path)
    resolved_source_ids = _load_resolved_source_ids([out_path, absence_out_path, discarded_path])

    correction_meta: dict[str, dict] = {}
    confirmed_count = reattributed_count = discarded_count = 0
    brand_filtered_count = 0
    for hyp in hypotheses:
        if hyp["custom_id"] in resolved_source_ids:
            continue

        # 2026-08-25: universal, not just for the guaranteed cross-referral
        # seeds — ANY candidate whose seed chunk carries a real brand gets
        # its classification retrieval run under that exact brand filter,
        # mirroring TextReplyController.reply's own gating byte-for-byte
        # (`if brand_filter and "brand" in allowed_metadata_keys`). Without
        # this, a brand-tagged candidate would be classified against
        # unfiltered retrieval — a different, less restrictive condition
        # than production actually applies whenever a patient has that
        # brand selected, which is exactly the fidelity gap this session
        # set out to close for Pass 1 as a whole.
        seed_brand = hyp.get("seed_brand")
        metadata_filters = None
        if seed_brand and "brand" in (client_config.allowed_metadata_keys or []):
            metadata_filters = {"brand": seed_brand}
            brand_filtered_count += 1

        results = await retrieval_controller.retrieve(
            client_id=client_id, query=hyp["instruction"], metadata_filters=metadata_filters,
            top_k_override=client_config.whatsapp_retrieval_top_k_broad,
        )
        if not results:
            _write_record(discarded_path, {
                "source_hypothesis_custom_id": hyp["custom_id"], "reason": "zero_retrieval_results",
            })
            discarded_count += 1
            continue

        top_chunk = results[0]["chunk"]
        retrieved_chunk_id = str(top_chunk.id)
        retrieved_field_data = dict((top_chunk.metadata_payload or {}).get("field_data") or {})

        if retrieved_chunk_id == hyp["seed_chunk_id"]:
            _write_final_narrow(out_path, hyp, content=top_chunk.content, field_data=retrieved_field_data)
            confirmed_count += 1
            continue

        if _field_data_matches(hyp["output_json"], retrieved_field_data):
            _write_final_narrow(out_path, hyp, content=top_chunk.content, field_data=retrieved_field_data)
            reattributed_count += 1
            continue

        custom_id = teacher.stable_id("narrow_correction", hyp["custom_id"], retrieved_chunk_id)
        correction_meta[custom_id] = {
            "hypothesis_custom_id": hyp["custom_id"],
            "instruction": hyp["instruction"],
            "retrieved_content": top_chunk.content,
            "retrieved_field_data": retrieved_field_data,
        }

    if hypotheses:
        print(
            f"    [narrow_classify] {confirmed_count} confirmed, {reattributed_count} reattributed "
            f"(real near-duplicate), {len(correction_meta)} flagged for correction, "
            f"{discarded_count} discarded (zero retrieval results), "
            f"{brand_filtered_count}/{len(hypotheses)} classified under a real brand filter"
        )

    # ---- Phase 1b: correction batch (only real, still-unresolved mismatches) ----
    # "already billed" (correction_path has a raw teacher result for this
    # custom_id) and "final write already landed" (out_path/absence_out_path
    # has it, i.e. it's in resolved_source_ids) are DIFFERENT questions —
    # correction_meta above already excludes fully-resolved hypotheses, but
    # a candidate can be billed WITHOUT its final write having landed (a
    # prior --fresh wiped out_path/absence_out_path without touching this
    # bookkeeping file). Conflating the two meant such a candidate was
    # skipped as "already done" without ever being rewritten — the real
    # 2026-08-25 bug this split fixes. Billed-but-unresolved candidates are
    # replayed from their existing raw record (zero new teacher cost);
    # only genuinely new candidates go through a fresh teacher call.
    existing_corrections = {rec["custom_id"]: rec for rec in _load_jsonl(correction_path)}

    def finalize_correction(custom_id: str, meta: dict, json_block, phrasing: str, raw_output: str) -> None:
        if isinstance(json_block, dict) and json_block:
            _write_record(out_path, {
                "custom_id": custom_id,
                "pass": "narrow_positive",
                "source_hypothesis_custom_id": meta["hypothesis_custom_id"],
                "instruction": meta["instruction"],
                "input": meta["retrieved_content"],
                "output_json": json_block,
                "output_phrasing": phrasing,
                "raw_output": raw_output,
                "grounding": {"type": "narrow", "field_data": meta["retrieved_field_data"]},
            })
        else:
            _write_record(absence_out_path, {
                "custom_id": f"narrow-mismatch-{custom_id}",
                "pass": "absence",
                "source_hypothesis_custom_id": meta["hypothesis_custom_id"],
                "instruction": meta["instruction"],
                "input": meta["retrieved_content"],
                "output_json": json_block,
                "output_phrasing": phrasing,
                "raw_output": raw_output,
                "grounding": {"type": "absence"},
            })

    replayed = 0
    for custom_id, meta in correction_meta.items():
        existing = existing_corrections.get(custom_id)
        if existing is not None:
            finalize_correction(custom_id, meta, existing["output_json"], existing["output_phrasing"], existing["raw_output"])
            replayed += 1
    if replayed:
        print(f"    [narrow_correction] replayed {replayed} already-billed correction(s) whose final write was missing")

    correction_new_meta = {cid: meta for cid, meta in correction_meta.items() if cid not in existing_corrections}

    def build_correction_item(custom_id: str, meta: dict) -> teacher.BatchRequestItem:
        return teacher.BatchRequestItem(
            custom_id=custom_id, system_prompt=system_prompt,
            user_message=teacher.build_verification_prompt(
                question=meta["instruction"], context_text=meta["retrieved_content"],
                allowed_keys_description=json.dumps(list(meta["retrieved_field_data"].keys()), ensure_ascii=False),
            ),
        )

    def write_correction(custom_id: str, meta: dict, result: teacher.TeacherCallResult | None):
        if result is None:
            return None
        cost_tracker.record("narrow_correction", result.input_tokens, result.output_tokens)
        if result.truncated():
            return None

        json_block, phrasing = teacher.extract_json_and_phrasing(result.raw_text)
        if json_block is None:
            return None

        _write_record(correction_path, {
            "custom_id": custom_id,
            "pass": "narrow_correction",
            "source_hypothesis_custom_id": meta["hypothesis_custom_id"],
            "instruction": meta["instruction"],
            "input": meta["retrieved_content"],
            "output_json": json_block,
            "output_phrasing": phrasing,
            "raw_output": result.raw_text,
        })
        finalize_correction(custom_id, meta, json_block, phrasing, result.raw_text)
        return meta["instruction"]

    if correction_new_meta or checkpoint.load_batch_state(state_path).get("narrow_correction") is not None:
        await _run_checkpointed_batch(
            client, pass_label="narrow_correction", out_path=correction_path, state_path=state_path,
            new_meta=correction_new_meta, build_item=build_correction_item, write_fn=write_correction,
            use_batch=_use_batch(limit, force_batch),
        )

    return checkpoint.load_all_instructions(out_path)


# ---------------------------------------------------------------------
# Pass 2 — Broad Positive
# ---------------------------------------------------------------------

async def run_pass2_broad_positive(
    *, client, working_set: list[ChunkRecord], retrieval_controller, client_config,
    client_id: str, cost_tracker, out_path, state_path, limit: int | None = None, force_batch: bool = False,
) -> list[str]:
    """Fires synthetic broad-style probe queries at the REAL, live
    RetrievalController and takes whatever 5 chunks it actually returns —
    highest fidelity, exercises real hybrid search + reranking rather
    than hand-assembling an approximation. One probe per distinct
    sheet_name × BROAD_PROBES_PER_SHEET. Does NOT try to force an
    artificial "one irrelevant chunk" or cross_brand_note scenario by
    hand — whatever the real reranker returns (on-topic or not) is
    exactly what gets used, same as production; if a returned source
    genuinely isn't relevant to the generated question, the teacher is
    instructed to leave that source's fields empty, and Stage 5 verifies
    that per-source exactly as it verifies every other source.

    Retrieval itself is local infra (embeddings + reranker), never a
    billed Claude call, so it always runs live/eagerly regardless of
    `limit` or batching — only the resulting teacher prompts get batched.
    Every probe still runs on a resumed run too (cheap, local, and the
    candidate SET isn't guaranteed bit-identical run to run) — only
    already-durably-written candidates (matched by their content-derived
    custom_id, not position) are skipped from re-submission."""
    by_sheet: dict[str, list[ChunkRecord]] = {}
    for record in working_set:
        if record.sheet_name:
            by_sheet.setdefault(record.sheet_name, []).append(record)

    candidates: list[dict] = []
    for sheet_name in by_sheet:
        for probe_index in range(BROAD_PROBES_PER_SHEET):
            if limit is not None and len(candidates) >= limit:
                break
            template = BROAD_PROBE_QUESTION_TEMPLATES[probe_index % len(BROAD_PROBE_QUESTION_TEMPLATES)]
            probe_query = template.format(sheet_name=sheet_name)

            results = await retrieval_controller.retrieve(
                client_id=client_id, query=probe_query,
                top_k_override=client_config.whatsapp_retrieval_top_k_broad,
            )
            if len(results) < 2:
                continue  # this probe didn't actually land a broad case — skip, never fabricate one

            source_blocks, per_source_field_data, allowed_keys_lines, source_chunk_ids = [], [], [], []
            for index, result in enumerate(results, start=1):
                chunk = result["chunk"]
                source_blocks.append(f"[BEGIN SOURCE {index}]\n{chunk.content}\n[END SOURCE {index}]")
                source_field_data = dict((chunk.metadata_payload or {}).get("field_data") or {})
                per_source_field_data.append(source_field_data)
                source_chunk_ids.append(str(chunk.id))
                allowed_keys_lines.append(
                    f"SOURCE {index}: {json.dumps(list(source_field_data.keys()), ensure_ascii=False)}"
                )
            candidates.append({
                "context_text": "\n\n".join(source_blocks),
                "per_source_field_data": per_source_field_data,
                "allowed_keys_lines": allowed_keys_lines,
                # sorted -> order-independent identity: the same set of
                # sources landing in a different order across runs is
                # still recognized as the same already-done candidate.
                "custom_id": teacher.stable_id("broad", *sorted(source_chunk_ids)),
            })
        if limit is not None and len(candidates) >= limit:
            break

    done_ids = checkpoint.load_done_custom_ids(out_path)
    new_meta: dict[str, dict] = {c["custom_id"]: c for c in candidates if c["custom_id"] not in done_ids}

    already_done = len(candidates) - len(new_meta)
    if already_done:
        print(f"    [broad_positive] {already_done}/{len(candidates)} already done — resuming, {len(new_meta)} new this run")

    system_prompt = _base_system_prompt()

    def build_item(custom_id: str, meta: dict) -> teacher.BatchRequestItem:
        return teacher.BatchRequestItem(
            custom_id=custom_id, system_prompt=system_prompt,
            user_message=teacher.build_generation_prompt(
                context_text=meta["context_text"],
                allowed_keys_description="\n".join(meta["allowed_keys_lines"])
                + '\n\nJSON format required: [{"source": 1, "fields": {...}}, {"source": 2, "fields": {...}}, ...] '
                "— one entry per source above, in order, `fields` empty {} for any source not actually relevant.",
            ),
            # Wider budget than the other passes — up to 5 per-source JSON
            # entries can eat most of the default before phrasing even
            # starts (this pipeline's own smoke test hit exactly that).
            max_tokens=teacher.BROAD_MAX_TOKENS,
        )

    def write_fn(custom_id: str, meta: dict, result: teacher.TeacherCallResult | None):
        if result is None:
            return None
        cost_tracker.record("broad_positive", result.input_tokens, result.output_tokens)
        if result.truncated():
            return None

        question, json_block, phrasing = teacher.extract_question_json_and_phrasing(result.raw_text)
        if question is None or not isinstance(json_block, list):
            return None

        _write_record(out_path, {
            "custom_id": custom_id,
            "pass": "broad_positive",
            "instruction": question,
            "input": meta["context_text"],
            "output_json": json_block,
            "output_phrasing": phrasing,
            "raw_output": result.raw_text,
            "grounding": {"type": "broad", "sources_field_data": meta["per_source_field_data"]},
        })
        return question

    if candidates:
        await _run_checkpointed_batch(
            client, pass_label="broad_positive", out_path=out_path, state_path=state_path,
            new_meta=new_meta, build_item=build_item, write_fn=write_fn,
            use_batch=_use_batch(limit, force_batch),
        )

    return checkpoint.load_all_instructions(out_path)


# ---------------------------------------------------------------------
# Pass 3 — Absence
# ---------------------------------------------------------------------

async def run_pass3_absence(
    *, client, working_set: list[ChunkRecord], question_pool: list[str], cost_tracker, out_path, state_path,
    count: int = ABSENCE_COUNT, limit: int | None = None, force_batch: bool = False,
) -> None:
    """Deliberately mismatched pairing: a real question from Pass 1/2's
    pool, paired with context built from an unrelated chunk (different
    sheet — guarantees no real overlap with what the question actually
    asks). This pairing is synthetic by construction, which is the whole
    point of the category — a real question distilled against a real
    chunk that actually answers it can never produce an Absence example,
    by definition, so this is the one pass that can't be "found" in the
    real data, only deliberately built from it.

    Unlike Pass 1/4's fully deterministic candidate lists, pairs here are
    drawn from a seeded PRNG loop rather than enumerated up front — so
    resumability draws NEW pairs (skipping any whose (question,
    unrelated_chunk) identity is already durably written) until reaching
    `effective_count` NEW candidates, rather than filtering a fixed list.
    A bounded attempt cap guards against an unlucky run of repeats."""
    if not question_pool or len(working_set) < 2:
        return

    effective_count = min(count, limit) if limit is not None else count
    done_ids = checkpoint.load_done_custom_ids(out_path)
    already_done_count = len(done_ids)
    needed = max(0, effective_count - already_done_count)

    rng = random.Random(FIXED_SEED + 1)
    new_meta: dict[str, dict] = {}
    attempts, max_attempts = 0, needed * 20 + 50
    while len(new_meta) < needed and attempts < max_attempts:
        attempts += 1
        question = rng.choice(question_pool)
        unrelated = rng.choice(working_set)
        custom_id = teacher.stable_id("absence", question, str(unrelated.chunk_id))
        if custom_id in done_ids or custom_id in new_meta:
            continue  # already processed in a prior run, or a repeat draw this run — try another pair
        # 2026-08-24: real full chunk content, not a reconstructed
        # "\n"-joined field_data dump — matches what a real 1-chunk
        # retrieval now hands production (§ Pass 1's own redesign note),
        # so an absence example's context has the same shape a real
        # irrelevant-chunk retrieval would actually produce.
        context_text = unrelated.content
        new_meta[custom_id] = {
            "question": question, "context_text": context_text,
            "allowed_keys_description": json.dumps(list(unrelated.field_data.keys()), ensure_ascii=False),
        }

    if already_done_count:
        print(f"    [absence] {already_done_count}/{effective_count} already done — resuming, {len(new_meta)} new this run")

    system_prompt = _base_system_prompt()

    def build_item(custom_id: str, meta: dict) -> teacher.BatchRequestItem:
        return teacher.BatchRequestItem(
            custom_id=custom_id, system_prompt=system_prompt,
            user_message=teacher.build_verification_prompt(
                question=meta["question"], context_text=meta["context_text"],
                allowed_keys_description=meta.get("allowed_keys_description", "[]"),
            ),
        )

    def write_fn(custom_id: str, meta: dict, result: teacher.TeacherCallResult | None):
        if result is None:
            return None
        cost_tracker.record("absence", result.input_tokens, result.output_tokens)
        if result.truncated():
            return None

        json_block, phrasing = teacher.extract_json_and_phrasing(result.raw_text)
        if json_block is None:
            return None

        _write_record(out_path, {
            "custom_id": custom_id,
            "pass": "absence",
            "instruction": meta["question"],
            "input": meta["context_text"],
            "output_json": json_block,
            "output_phrasing": phrasing,
            "raw_output": result.raw_text,
            "grounding": {"type": "absence"},
        })
        return True

    if new_meta:
        await _run_checkpointed_batch(
            client, pass_label="absence", out_path=out_path, state_path=state_path,
            new_meta=new_meta, build_item=build_item, write_fn=write_fn,
            use_batch=_use_batch(limit, force_batch),
        )
    elif checkpoint.load_batch_state(state_path).get("absence") is not None:
        await _run_checkpointed_batch(
            client, pass_label="absence", out_path=out_path, state_path=state_path,
            new_meta={}, build_item=build_item, write_fn=write_fn,
            use_batch=_use_batch(limit, force_batch),
        )


# ---------------------------------------------------------------------
# Pass 4 — Ambiguous
# ---------------------------------------------------------------------

def _build_ambiguous_direct_candidates(working_set: list[ChunkRecord], count: int) -> list[tuple[ChunkRecord, str]]:
    """Sub-case (a): a field genuinely blank for THIS row. ChunkingController
    only ever puts non-empty fields into field_data (§3.8's
    _extract_non_empty_fields), so a blank value is never present-with-
    empty-string — it's simply absent from the dict. The real signal is:
    a column that OTHER rows of the same sheet do carry, that this
    particular record doesn't."""
    columns_by_sheet: dict[str, set] = {}
    for record in working_set:
        if record.sheet_name:
            columns_by_sheet.setdefault(record.sheet_name, set()).update(record.field_data.keys())

    candidates: list[tuple[ChunkRecord, str]] = []
    for record in working_set:
        if not record.sheet_name:
            continue
        # sorted(): a bare set's iteration order depends on Python's
        # per-process string hash seed (randomized by default, not fixed
        # across separate `python script.py` invocations) — without this,
        # the "deterministic" candidate list silently isn't reproducible
        # across process restarts, which is exactly what resumability
        # depends on.
        for missing_field in sorted(columns_by_sheet[record.sheet_name] - set(record.field_data.keys())):
            candidates.append((record, missing_field))

    random.Random(FIXED_SEED + 2).shuffle(candidates)
    return candidates[:count]


def _build_ambiguous_cross_chunk_candidates(
    working_set: list[ChunkRecord], count: int,
) -> list[tuple[str, list, list[ChunkRecord]]]:
    """Sub-case (b): the real, documented pattern (the Lucky Card
    eligibility sheet's several scenarios sharing one field label with
    different values) — multiple real chunks reporting genuinely
    different, normalized-distinct values under the same field label.
    A GROUP BY over the whole working set, not a per-row check."""
    by_label: dict[str, dict[str, list]] = {}
    for record in working_set:
        for label, value in record.field_data.items():
            normalized = str(value).strip()
            by_label.setdefault(label, {}).setdefault(normalized, []).append(record)

    conflicts = [(label, groups) for label, groups in by_label.items() if len(groups) >= 2]
    random.Random(FIXED_SEED + 3).shuffle(conflicts)

    candidates = []
    for label, value_groups in conflicts[:count]:
        distinct_values = list(value_groups.keys())[:2]
        conflicting_records = [value_groups[v][0] for v in distinct_values]
        candidates.append((label, distinct_values, conflicting_records))
    return candidates


async def run_pass4_ambiguous(
    *, client, working_set: list[ChunkRecord], cost_tracker, out_path, state_path,
    direct_count: int = AMBIGUOUS_DIRECT_COUNT, cross_chunk_count: int = AMBIGUOUS_CROSS_CHUNK_COUNT,
    limit: int | None = None, force_batch: bool = False,
) -> None:
    """`limit`, when set, caps EACH sub-case independently (direct and
    cross-chunk are structurally different generators — a smoke test
    should exercise both, not spend its whole budget on one). Both sub-
    cases are submitted as ONE combined batch — they're independent of
    each other, so there's no reason to pay two separate batch-scheduling
    round trips for one pass. Both sub-cases' candidate lists are fully
    deterministic (seeded shuffle over real DB data), so resumability is
    a straightforward filter-by-already-done-custom_id, same as Pass 1."""
    system_prompt = _base_system_prompt()
    effective_direct = min(direct_count, limit) if limit is not None else direct_count
    effective_cross_chunk = min(cross_chunk_count, limit) if limit is not None else cross_chunk_count

    direct_candidates = _build_ambiguous_direct_candidates(working_set, effective_direct)
    cross_chunk_candidates = _build_ambiguous_cross_chunk_candidates(working_set, effective_cross_chunk)

    done_ids = checkpoint.load_done_custom_ids(out_path)
    new_meta: dict[str, dict] = {}
    total_candidates = len(direct_candidates) + len(cross_chunk_candidates)

    for record, missing_field in direct_candidates:
        custom_id = teacher.stable_id("ambiguous_direct", str(record.chunk_id), missing_field)
        if custom_id in done_ids:
            continue
        # 2026-08-24: real full chunk content, not a reconstructed
        # "\n"-joined field_data dump — same real-shape parity as Pass 1
        # and Pass 3 above (ambiguous_cross_chunk already used the real
        # `.content` via its [BEGIN SOURCE n] wrapping, so this brings
        # ambiguous_direct in line with its own sibling sub-case too).
        context_text = record.content
        new_meta[custom_id] = {
            "kind": "ambiguous_direct", "context_text": context_text,
            "field_data": dict(record.field_data), "target_field": missing_field,
        }

    for label, distinct_values, conflicting_records in cross_chunk_candidates:
        custom_id = teacher.stable_id(
            "ambiguous_cross_chunk", label, *sorted(str(r.chunk_id) for r in conflicting_records),
        )
        if custom_id in done_ids:
            continue
        context_text = "\n\n".join(
            f"[BEGIN SOURCE {i}]\n{r.content}\n[END SOURCE {i}]"
            for i, r in enumerate(conflicting_records, start=1)
        )
        new_meta[custom_id] = {
            "kind": "ambiguous_cross_chunk", "context_text": context_text,
            "label": label, "conflicting_values": distinct_values,
        }

    already_done = total_candidates - len(new_meta)
    if already_done:
        print(f"    [ambiguous] {already_done}/{total_candidates} already done — resuming, {len(new_meta)} new this run")

    def build_item(custom_id: str, meta: dict) -> teacher.BatchRequestItem:
        if meta["kind"] == "ambiguous_direct":
            user_message = teacher.build_generation_prompt_for_missing_field(
                context_text=meta["context_text"], missing_field=meta["target_field"],
            )
        else:
            user_message = teacher.build_generation_prompt_for_conflict(
                context_text=meta["context_text"], label=meta["label"],
            )
        return teacher.BatchRequestItem(custom_id=custom_id, system_prompt=system_prompt, user_message=user_message)

    def write_fn(custom_id: str, meta: dict, result: teacher.TeacherCallResult | None):
        if result is None:
            return None
        cost_tracker.record(meta["kind"], result.input_tokens, result.output_tokens)
        if result.truncated():
            return None

        question, json_block, phrasing = teacher.extract_question_json_and_phrasing(result.raw_text)
        if question is None or json_block is None:
            return None

        if meta["kind"] == "ambiguous_direct":
            _write_record(out_path, {
                "custom_id": custom_id,
                "pass": "ambiguous_direct",
                "instruction": question,
                "input": meta["context_text"],
                "output_json": json_block,
                "output_phrasing": phrasing,
                "raw_output": result.raw_text,
                "grounding": {
                    "type": "ambiguous_direct",
                    "field_data": meta["field_data"],
                    "target_field": meta["target_field"],
                },
            })
        else:
            _write_record(out_path, {
                "custom_id": custom_id,
                "pass": "ambiguous_cross_chunk",
                "instruction": question,
                "input": meta["context_text"],
                "output_json": json_block,
                "output_phrasing": phrasing,
                "raw_output": result.raw_text,
                "grounding": {
                    "type": "ambiguous_cross_chunk",
                    "label": meta["label"],
                    "conflicting_values": meta["conflicting_values"],
                },
            })
        return True

    if new_meta or checkpoint.load_batch_state(state_path).get("ambiguous") is not None:
        await _run_checkpointed_batch(
            client, pass_label="ambiguous", out_path=out_path, state_path=state_path,
            new_meta=new_meta, build_item=build_item, write_fn=write_fn,
            use_batch=_use_batch(limit, force_batch),
        )
