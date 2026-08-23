#!/usr/bin/env python3
"""Admin CLI: collect real API responses for the golden test suite, and
grade them against each case's expected_fact.

Sends each `patient_query` in golden_test_suite.json to the live
POST /api/whatsapp/chat endpoint (a real HTTP call, same as Postman —
never a direct import of TextReplyController/IntentRoutingController,
per claude.md §4.3's "never bypass the real layers" testing policy) and
saves the response alongside the original test case, together with a
grounding-check verdict against expected_fact.

Grading (deliberate scope change from this script's original "collection
only, no accuracy math" boundary — the ChatResponse.debug_json field this
grades against didn't exist before TextReplyController started splitting
the fine-tuned model's JSON-then-phrasing output): expected_fact entries
follow the same "label: value[. label2: value2...]" convention
ChunkingController itself concatenates fields in (claude.md §3.8) — see
_extract_expected_pairs. Each extracted (label, value) pair is checked
against debug_json's own keys/values — see _grade_case for the exact
matching rules and why label matching is substring-tolerant but value
matching is NOT (a real, dangerous trap: "متاح" (available) is literally
a substring of "غير متاح" (NOT available), so a substring check on
VALUES would silently PASS an inverted fact — the single most common
real failure mode this whole suite exists to catch, per this session's
own review of golden_outputs_nilechat12b_1.json).

This grading is intentionally conservative, not a strict pass/fail
gate: PASS and FAIL are only ever assigned on real, exact evidence,
everything else is NEEDS_REVIEW rather than a guess. Treat a NEEDS_REVIEW
verdict as "a human should look at this," never as an implicit pass.

Usage:
    python scripts/collect_golden_responses.py
    python scripts/collect_golden_responses.py --base-url http://localhost:8000 --api-key raylab-admin-test-key
    python scripts/collect_golden_responses.py --output golden_outputs_falcon34b.json
    python scripts/collect_golden_responses.py --retry-failed golden_outputs_falcon34b.json

Runs from anywhere — it locates golden_test_suite.json relative to its
own file location (repo root, one level above scripts/).

Phase 0 bake-off note: pass --output with a model-specific filename for
each candidate run (see README's Phase 0 section) — the default filename
is unversioned by design (mirrors the original single-model workflow),
so grading multiple candidates without --output will silently overwrite
the previous candidate's saved output.

--retry-failed note: this script has no caching/skip logic for a normal
run — every invocation re-sends all cases. --retry-failed is the
targeted alternative: it loads an existing output file, re-sends ONLY
the cases whose actual_reply looks like a failure ([REQUEST FAILED...]
or missing), re-grades just those, and merges the fresh results back in
— successful cases are left untouched, not re-sent or re-graded, so
retrying doesn't reintroduce fresh latency-variance failures on cases
that already succeeded.
"""

import argparse
import json
import re
import uuid
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
INPUT_PATH = SCRIPT_DIR.parent / "golden_test_suite.json"
DEFAULT_OUTPUT_PATH = SCRIPT_DIR / "golden_raw_outputs.json"

_DOCUMENT_PREFIX_RE = re.compile(r"^\[Document:[^\]]*\]\s*")


def _normalize(value) -> str:
    return re.sub(r"\s+", " ", str(value).strip())


def _extract_expected_pairs(expected_fact: str) -> list[tuple[str, str]]:
    """Best-effort split of expected_fact into (label, value) pairs,
    mirroring the exact "label: value. label2: value2..." shape
    ChunkingController's own _concatenate_fields produces (claude.md
    §3.8) — most expected_fact entries were transcribed directly from
    that format. A clause with no ": " (a free-text sentence, not a
    field) contributes no pair — that's correct, not a failure to fix;
    plenty of real cases (e.g. multi-step prep instructions) are
    genuinely prose, not label:value data, and grading those
    automatically would mean guessing, not checking."""
    text = _DOCUMENT_PREFIX_RE.sub("", expected_fact.strip())
    pairs = []
    for clause in text.split(". "):
        if ": " not in clause:
            continue
        label, _, value = clause.partition(": ")
        label, value = label.strip(), value.strip()
        if label and value:
            pairs.append((label, value))
    return pairs


def _flatten_debug_json(debug_json) -> dict:
    """debug_json is a dict for a narrow-breadth turn, or a list of
    {"source": n, "fields": {...}} for a broad one (see
    routes/schemes/whatsapp.py's own field docstring) — merged into one
    {label: value} dict here since grading only needs "was this value
    reported by the model anywhere in its answer," not which source it
    came from. None/malformed input yields an empty dict, never raises."""
    if isinstance(debug_json, dict):
        return debug_json
    if isinstance(debug_json, list):
        flattened = {}
        for entry in debug_json:
            if isinstance(entry, dict):
                flattened.update(entry.get("fields") or {})
        return flattened
    return {}


def _grade_case(expected_fact: str, debug_json) -> dict:
    """Returns {"verdict": "PASS"|"FAIL"|"NEEDS_REVIEW", "reason": str,
    "checked_pairs": [...]}."

    Matching rules, deliberately asymmetric between label and value:
    - LABEL matching is substring-tolerant in either direction — real
      expected_fact wording and the model's own field labels vary
      slightly, and negation words don't live in label text in this
      dataset (they live in values: "متاح"/"غير متاح").
    - VALUE matching is EXACT (post-normalization) only, never
      substring. "متاح" (available) is literally a substring of
      "غير متاح" (NOT available) — a substring check here would silently
      PASS an inverted fact, the single most common real failure mode
      this whole suite exists to catch (see this session's own review of
      golden_outputs_nilechat12b_1.json). Exact match is the only safe
      check for a value comparison in this language.

    A single confidently-mismatched pair (same field, different value)
    fails the whole case — one contradiction is enough to flag. Absence
    of debug_json, or expected_fact having no extractable pairs at all,
    is NEEDS_REVIEW, never an assumed pass."""
    if debug_json is None:
        return {"verdict": "NEEDS_REVIEW", "reason": "no debug_json (plain-text output, or Mode B/out-of-domain turn)", "checked_pairs": []}

    expected_pairs = _extract_expected_pairs(expected_fact)
    if not expected_pairs:
        return {"verdict": "NEEDS_REVIEW", "reason": "expected_fact has no extractable label:value pairs (likely free-text prose)", "checked_pairs": []}

    flat = _flatten_debug_json(debug_json)
    normalized_actual = {_normalize(k): _normalize(v) for k, v in flat.items()}

    checked = []
    any_fail = False
    all_pass = True
    for label, value in expected_pairs:
        norm_label, norm_value = _normalize(label), _normalize(value)
        matched_key = next(
            (k for k in normalized_actual if norm_label in k or k in norm_label),
            None,
        )
        if matched_key is None:
            checked.append({"label": label, "expected_value": value, "result": "key_not_found"})
            all_pass = False
            continue

        actual_value = normalized_actual[matched_key]
        if actual_value == norm_value:
            checked.append({"label": label, "expected_value": value, "actual_value": actual_value, "result": "match"})
        else:
            checked.append({"label": label, "expected_value": value, "actual_value": actual_value, "result": "value_mismatch"})
            any_fail = True
            all_pass = False

    if any_fail:
        return {"verdict": "FAIL", "reason": "at least one matched field reported a different value", "checked_pairs": checked}
    if all_pass:
        return {"verdict": "PASS", "reason": "every extractable expected pair matched exactly", "checked_pairs": checked}
    return {"verdict": "NEEDS_REVIEW", "reason": "some expected fields weren't found in debug_json (label-wording mismatch or model omission — can't tell which)", "checked_pairs": checked}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect live /api/whatsapp/chat responses for every case in golden_test_suite.json and grade them.",
    )
    parser.add_argument("--base-url", default="http://localhost:8000", help="FastAPI server base URL")
    parser.add_argument("--api-key", default="raylab-admin-test-key", dest="api_key", help="X-Admin-Api-Key header value")
    parser.add_argument(
        "--timeout", type=int, default=260,
        help="Per-request timeout in seconds. Must stay above the backend's own "
             "GENERATION_REQUEST_TIMEOUT_SECONDS (currently 120) times up to 2 sequential LLM "
             "calls per turn (intent classification + reply generation) — raised from the "
             "original 60 for exactly this reason during Phase 0's bake-off (see README): a 60s "
             "outer timeout was firing before a slower model's own, larger internal timeout ever "
             "got a chance to succeed or fail cleanly.",
    )
    parser.add_argument(
        "--output", default=None,
        help="Output filename, written under scripts/. Defaults to golden_raw_outputs.json "
             "(overwritten each run) — pass a model-specific name for bake-off runs, "
             "e.g. golden_outputs_falcon34b.json.",
    )
    parser.add_argument(
        "--retry-failed", default=None, dest="retry_failed",
        help="Path (under scripts/) to a previous output file. Only re-sends and re-grades cases "
             "whose actual_reply indicates a failed request, merges the fresh results back in, and "
             "writes to --output (defaults to the same file, updated in place).",
    )
    return parser.parse_args()


def _looks_failed(actual_reply) -> bool:
    return not actual_reply or str(actual_reply).startswith("[REQUEST FAILED")


def _send_one(url: str, headers: dict, timeout: int, case: dict) -> dict:
    session_id = str(uuid.uuid4())
    body = {"session_id": session_id, "message": case["patient_query"]}
    try:
        response = requests.post(url, headers=headers, json=body, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
        actual_reply = payload.get("reply")
        debug_json = payload.get("debug_json")
    except requests.exceptions.RequestException as e:
        # One bad case (timeout, 500, connection error against the real
        # remote model) must not abort the whole collection run —
        # recorded as an error string so it's still visible for review,
        # not silently dropped from the output.
        actual_reply = f"[REQUEST FAILED: {e}]"
        debug_json = None

    result = {**case, "session_id": session_id, "actual_reply": actual_reply, "debug_json": debug_json}
    if _looks_failed(actual_reply):
        result["verdict"] = "NEEDS_REVIEW"
        result["verdict_reason"] = "request failed — no reply to grade"
        result["checked_pairs"] = []
    else:
        result.update(_grade_case(case["expected_fact"], debug_json))
        result["verdict_reason"] = result.pop("reason")
    return result


def _print_summary(results: list[dict]) -> None:
    counts = {"PASS": 0, "FAIL": 0, "NEEDS_REVIEW": 0}
    for case in results:
        counts[case.get("verdict", "NEEDS_REVIEW")] = counts.get(case.get("verdict", "NEEDS_REVIEW"), 0) + 1

    print("\n" + "=" * 64)
    print("Golden suite grading summary")
    print("=" * 64)
    print(f"  PASS:          {counts['PASS']:3d}")
    print(f"  FAIL:          {counts['FAIL']:3d}")
    print(f"  NEEDS_REVIEW:  {counts['NEEDS_REVIEW']:3d}")
    print(f"  TOTAL:         {len(results):3d}")
    if counts["FAIL"]:
        print("\n  Failed cases:")
        for case in results:
            if case.get("verdict") == "FAIL":
                print(f"    [{case['sheet_name']}] {case['patient_query'][:60]!r}")
    print("=" * 64 + "\n")


def main() -> int:
    args = parse_args()
    url = f"{args.base_url.rstrip('/')}/api/whatsapp/chat"
    headers = {
        "Content-Type": "application/json",
        "X-Admin-Api-Key": args.api_key,
    }

    if args.retry_failed:
        retry_path = SCRIPT_DIR / args.retry_failed
        with open(retry_path, encoding="utf-8") as f:
            results = json.load(f)
        output_path = SCRIPT_DIR / args.output if args.output else retry_path

        retry_indices = [i for i, case in enumerate(results) if _looks_failed(case.get("actual_reply"))]
        if not retry_indices:
            print(f"No failed cases found in {retry_path} — nothing to retry.")
            _print_summary(results)
            return 0

        print(f"Retrying {len(retry_indices)}/{len(results)} previously-failed case(s) from {retry_path}")
        for n, i in enumerate(retry_indices, start=1):
            case = results[i]
            print(f"[{n}/{len(retry_indices)}] {case['sheet_name']!r}: {case['patient_query'][:50]!r}")
            results[i] = _send_one(url, headers, args.timeout, case)

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)

        still_failed = sum(1 for case in results if _looks_failed(case.get("actual_reply")))
        print(f"\nDone — {len(results)} total responses saved to {output_path} ({still_failed} still failed)")
        _print_summary(results)
        return 0

    output_path = SCRIPT_DIR / args.output if args.output else DEFAULT_OUTPUT_PATH

    with open(INPUT_PATH, encoding="utf-8") as f:
        golden_cases = json.load(f)

    results = []
    total = len(golden_cases)
    for index, case in enumerate(golden_cases, start=1):
        print(f"[{index}/{total}] {case['sheet_name']!r}: {case['patient_query'][:50]!r}")
        results.append(_send_one(url, headers, args.timeout, case))

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    print(f"\nDone — {len(results)} responses saved to {output_path}")
    _print_summary(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
