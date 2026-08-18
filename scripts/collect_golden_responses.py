#!/usr/bin/env python3
"""Admin CLI: collect real API responses for the golden test suite.

Sends each `patient_query` in golden_test_suite.json to the live
POST /api/whatsapp/chat endpoint (a real HTTP call, same as Postman —
never a direct import of TextReplyController/IntentRoutingController,
per claude.md §4.3's "never bypass the real layers" testing policy) and
saves the raw response alongside the original test case. Pure collection
only — no scoring, no LLM-as-judge, no accuracy math. That evaluation
step happens separately, against this file's output.

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
run — every invocation re-sends all 67 cases. --retry-failed is the
targeted alternative: it loads an existing output file, re-sends ONLY
the cases whose actual_reply looks like a failure ([REQUEST FAILED...]
or missing), and merges the fresh results back in — successful cases are
left untouched, not re-sent, so retrying doesn't reintroduce fresh
latency-variance failures on cases that already succeeded.
"""

import argparse
import json
import uuid
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
INPUT_PATH = SCRIPT_DIR.parent / "golden_test_suite.json"
DEFAULT_OUTPUT_PATH = SCRIPT_DIR / "golden_raw_outputs.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect live /api/whatsapp/chat responses for every case in golden_test_suite.json.",
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
        help="Path (under scripts/) to a previous output file. Only re-sends cases whose "
             "actual_reply indicates a failed request, merges the fresh results back in, and "
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
        actual_reply = response.json().get("reply")
    except requests.exceptions.RequestException as e:
        # One bad case (timeout, 500, connection error against the real
        # remote model) must not abort the whole collection run —
        # recorded as an error string so it's still visible for review,
        # not silently dropped from the output.
        actual_reply = f"[REQUEST FAILED: {e}]"
    return {**case, "session_id": session_id, "actual_reply": actual_reply}


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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
