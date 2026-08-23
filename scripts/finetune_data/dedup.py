"""Stage 5.5 — deduplicates gate-accepted records before the stratified
train/val split (format_alpaca.py's split_stratified operates on these
same raw-accepted records, pre-format_record, so this runs on the
identical shape: `instruction`, `input`, `output_json`, `output_phrasing`,
still carrying `pass`).

Two independent, deliberately separate passes, not one combined check:

1. Exact (instruction, input, output_json, output_phrasing) duplicates —
   zero new training signal, just redundant weight on one example.
2. (instruction, input) duplicates alone — catches NEAR-twins where two
   different real chunks happen to share an identical field value and the
   teacher independently generated near-identical (but not byte-identical)
   phrasing for both. This MUST run before split_stratified(): the split
   has no content-awareness, so two near-twins can otherwise land on
   opposite sides of train/val — a real, if small, val-leakage risk.

Pass 2 is strictly a superset of what pass 1 catches (identical output
implies identical instruction+input), so running pass 1 first only
affects the reported stats, not the final kept set — kept separate
because "duplicate" and "near-twin sacrificed for split-safety" are
different findings worth surfacing separately, not because the mechanics
require two passes.

Both passes keep the FIRST occurrence and drop the rest, deterministic
given the same raw_*.jsonl input (which run_gate iterates in a fixed
file order, and each file in its own write order)."""

import json


def dedup_records(accepted: list[dict]) -> tuple[list[dict], dict]:
    stats = {"input": len(accepted), "exact_duplicates_dropped": 0, "near_twin_duplicates_dropped": 0}

    seen_exact = set()
    pass1: list[dict] = []
    for record in accepted:
        key = (
            record["instruction"], record["input"],
            json.dumps(record["output_json"], ensure_ascii=False, sort_keys=True),
            record["output_phrasing"],
        )
        if key in seen_exact:
            stats["exact_duplicates_dropped"] += 1
            continue
        seen_exact.add(key)
        pass1.append(record)

    seen_pair = set()
    pass2: list[dict] = []
    for record in pass1:
        pair_key = (record["instruction"], record["input"])
        if pair_key in seen_pair:
            stats["near_twin_duplicates_dropped"] += 1
            continue
        seen_pair.add(pair_key)
        pass2.append(record)

    stats["output"] = len(pass2)
    return pass2, stats


def print_dedup_report(stats: dict) -> None:
    print("\n" + "=" * 64)
    print("Stage 5.5 — deduplication report")
    print("=" * 64)
    print(f"  input records:                {stats['input']:4d}")
    print(f"  exact duplicates dropped:     {stats['exact_duplicates_dropped']:4d}")
    print(f"  near-twin duplicates dropped: {stats['near_twin_duplicates_dropped']:4d}  (same instruction+input, different phrasing — dropped to prevent train/val leakage)")
    print(f"  output records:               {stats['output']:4d}")
    print("=" * 64 + "\n")
