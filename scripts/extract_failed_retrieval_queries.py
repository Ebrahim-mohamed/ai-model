#!/usr/bin/env python3
"""Admin CLI: extract a real, evidence-based test set of known retrieval
failures from golden_outputs_nilechat12b_v2_after_improve_chunk_format_only.json
correlated against rag_execution.log's real retrieved-context blocks, for
batch A/B testing CrossEncoderProvider vs. BGERerankerV2M3Provider.

Two independently-computed, evidence-grounded signals (never a guess at
"this chunk is wrong" without real basis):

1. EMPTY_OR_APOLOGY — debug_json is empty/null AND actual_reply matches a
   real apology/decline phrase pattern (drawn directly from this file's own
   observed cases, not invented: "مش متوفرة", "مش واضحة", "للأسف",
   "أحولك لزميلي", etc.). Checked against expected_behavior implicitly by
   construction — every one of this file's real empty/null-debug_json +
   apology-text records has an expected_behavior describing a real,
   in-scope, substantive fact that should have been stated, confirmed by
   direct inspection before this script was written (two empty-debug_json
   records that were actually correct plain-text answers, not apologies,
   were manually checked and correctly excluded by the apology-text
   condition alone).

2. CONTEXT_TOPIC_MISMATCH — the real retrieved CONTEXT block for this
   exact query (pulled from rag_execution.log's own
   `[context] query=... full_context_block=` entries, the same real log
   this project's own manual investigations have used throughout) does not
   contain the expected_fact's own significant content words anywhere.
   Refined into two reasons: CONTEXT_TOPIC_MISMATCH (no real overlap at
   all — a genuine domain miss) vs. KEYWORD_COLLISION_SUSPECTED (the
   context DOES contain brand/generic terms the query itself used, but
   still misses the expected_fact's real content — the exact "غرفة PET CT"
   pattern this whole investigation started from).

Records with NO matching log entry (this golden run predates the current
log file, or was replayed through a different harness) are still
included if signal 1 alone applies — that signal doesn't need the log —
but are separately marked `context_available: false` for transparency,
never silently dropped or backfilled with an invented context snippet.

A one-shot admin script (claude.md §1.1) — lives under scripts/, never
imported by src/ at runtime. Read-only: touches neither the golden
outputs file nor the log.

Usage:
    python scripts/extract_failed_retrieval_queries.py
    python scripts/extract_failed_retrieval_queries.py --golden-file ... --log-file ... --out ...
"""

import argparse
import json
import re
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_GOLDEN_FILE = SCRIPT_DIR / "golden_outputs_nilechat12b_v2_after_improve_chunk_format_only.json"
DEFAULT_LOG_FILE = SCRIPT_DIR.parent / "src" / "rag_execution.log"
DEFAULT_OUT_FILE = SCRIPT_DIR / "failed_retrieval_queries_test_set.json"

# Real, directly-observed apology/decline phrases — every one of these
# appears verbatim in this project's own real Postman/golden-suite traffic
# this session, not invented. Spelling variants (ة/ه, و/ؤ) included since
# real output shows both.
APOLOGY_PHRASES = [
    "مش متوفرة عندي",
    "مش متوفره عندي",
    "مش واضحة عندي",
    "مش واضح عندي",
    "مش واضحة بالشكل",
    "مش واضح بالشكل",
    "للأسف يا فندم",
    "أحولك لزميلي",
    "أحوّلك لزميلي",
    "احولك لزميلي",
    "مش هقدر أأكدلك",
    "مش هقدر اأكدلك",
    "مش هقدر أقولك",
]

# Real brand/generic terms this project's own queries repeatedly use —
# used only to distinguish "topic mismatch with keyword collision" from
# "topic mismatch, no collision" among already-flagged CONTEXT_TOPIC_
# MISMATCH cases, never to decide the mismatch itself.
BRAND_OR_GENERIC_TERMS = ["كايروسكان", "تكنوسكان", "متاح", "فرع", "الفروع"]

LOG_LINE_START_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} ")
CONTEXT_QUERY_RE = re.compile(r"\[context\] query='(.*)' full_context_block=$")

# Arabic tokenizer for the expected_fact overlap check — strips the
# decorative tatweel/spacing this project's own source data sometimes uses
# (see TextReplyController._normalize_context_text's own docstring for the
# same real issue) and splits on whitespace/punctuation.
_TATWEEL_RE = re.compile("ـ")
_TOKEN_RE = re.compile(r"[^\W\d_]+", re.UNICODE)
_STOPWORDS = {
    "في", "من", "على", "الى", "إلى", "عن", "مع", "أو", "او", "و", "ال",
    "هل", "لا", "غير", "متاح", "متاحة", "بتاعه", "بتاعت",
}


def _normalize(text: str) -> str:
    return _TATWEEL_RE.sub("", text or "")


def _significant_tokens(text: str) -> set[str]:
    tokens = _TOKEN_RE.findall(_normalize(text))
    return {t for t in tokens if len(t) >= 3 and t not in _STOPWORDS}


def parse_log_context_blocks(log_path: Path) -> dict[str, list[str]]:
    """Returns {query_text: [context_block_text, ...]} — a query can
    legitimately appear more than once across multiple test runs; all
    occurrences are kept (caller uses the most recent one, but the count
    itself is useful signal too)."""
    if not log_path.exists():
        print(f"WARNING: log file not found at {log_path} -- proceeding with signal 1 (EMPTY_OR_APOLOGY) only.")
        return {}

    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    blocks: dict[str, list[str]] = {}

    i = 0
    while i < len(lines):
        match = CONTEXT_QUERY_RE.search(lines[i])
        if match:
            query_text = match.group(1)
            i += 1
            content_lines = []
            while i < len(lines) and not LOG_LINE_START_RE.match(lines[i]):
                content_lines.append(lines[i])
                i += 1
            blocks.setdefault(query_text, []).append("\n".join(content_lines))
        else:
            i += 1

    return blocks


def classify_record(record: dict, context_blocks_by_query: dict[str, list[str]]) -> dict | None:
    patient_query = record.get("patient_query", "")
    debug_json = record.get("debug_json")
    actual_reply = record.get("actual_reply", "") or ""
    expected_fact = record.get("expected_fact", "") or ""

    reasons = []

    # --- Signal 1: EMPTY_OR_APOLOGY ---
    is_empty_debug_json = debug_json in ({}, None, [])
    matched_phrase = next((p for p in APOLOGY_PHRASES if p in actual_reply), None)
    if is_empty_debug_json and matched_phrase:
        reasons.append(f"EMPTY_OR_APOLOGY (matched phrase: {matched_phrase!r})")

    # --- Signal 2: retrieved-context correlation ---
    matches = context_blocks_by_query.get(patient_query, [])
    # Fall back to a normalized match if an exact one wasn't found —
    # trailing/leading whitespace differences are the only normalization
    # applied, never a fuzzy/semantic match that could misattribute a
    # different query's context to this record.
    if not matches:
        normalized_target = patient_query.strip()
        for logged_query, blocks in context_blocks_by_query.items():
            if logged_query.strip() == normalized_target:
                matches = blocks
                break

    context_available = bool(matches)
    context_snippet = None
    if context_available:
        latest_context = matches[-1]
        context_snippet = latest_context[:1500]
        expected_tokens = _significant_tokens(expected_fact)
        context_tokens = _significant_tokens(latest_context)
        if expected_tokens and not (expected_tokens & context_tokens):
            context_has_brand_terms = any(term in latest_context for term in BRAND_OR_GENERIC_TERMS)
            if context_has_brand_terms:
                reasons.append("KEYWORD_COLLISION_SUSPECTED (context shares brand/generic terms with the "
                                "query but none of expected_fact's real content)")
            else:
                reasons.append("CONTEXT_TOPIC_MISMATCH (expected_fact's content not found anywhere in the "
                                "real retrieved context)")

    if not reasons:
        return None

    return {
        "patient_query": patient_query,
        "brand_filter": record.get("brand_filter"),
        "sheet_name": record.get("sheet_name"),
        "expected_fact": expected_fact,
        "expected_behavior": record.get("expected_behavior"),
        "actual_reply": actual_reply,
        "session_id": record.get("session_id"),
        "failure_reasons": reasons,
        "context_available": context_available,
        "context_match_count": len(matches),
        "retrieved_context_snippet": context_snippet,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--golden-file", type=Path, default=DEFAULT_GOLDEN_FILE)
    parser.add_argument("--log-file", type=Path, default=DEFAULT_LOG_FILE)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT_FILE)
    args = parser.parse_args()

    with open(args.golden_file, encoding="utf-8") as f:
        golden_records = json.load(f)

    context_blocks_by_query = parse_log_context_blocks(args.log_file)

    failed = []
    for record in golden_records:
        result = classify_record(record, context_blocks_by_query)
        if result is not None:
            failed.append(result)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(failed, f, ensure_ascii=False, indent=2)

    empty_or_apology = sum(1 for r in failed if any("EMPTY_OR_APOLOGY" in x for x in r["failure_reasons"]))
    keyword_collision = sum(1 for r in failed if any("KEYWORD_COLLISION" in x for x in r["failure_reasons"]))
    topic_mismatch = sum(1 for r in failed if any("CONTEXT_TOPIC_MISMATCH" in x for x in r["failure_reasons"]))
    no_context = sum(1 for r in failed if not r["context_available"])

    print(f"Scanned {len(golden_records)} golden records.")
    print(f"Flagged {len(failed)} as known retrieval/answer failures -> {args.out}")
    print(f"  EMPTY_OR_APOLOGY:            {empty_or_apology}")
    print(f"  KEYWORD_COLLISION_SUSPECTED: {keyword_collision}")
    print(f"  CONTEXT_TOPIC_MISMATCH:      {topic_mismatch}")
    print(f"  (of which, no log context found for {no_context} record(s) -- flagged on "
          f"signal 1 alone, no retrieved-context snippet available)")


if __name__ == "__main__":
    main()
