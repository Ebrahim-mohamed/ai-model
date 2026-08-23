"""Crash/disconnect/out-of-funds resumability for the data-generation
pipeline. Two independent mechanisms, both required for a genuine
"resume exactly where it left off" guarantee — one alone isn't enough:

1. Already-written-record dedup (load_done_custom_ids). Every record
   sampling.py writes carries a stable, CONTENT-derived `custom_id` (see
   teacher.stable_id) — a hash of the real chunk/label/question identity
   that produced it, never a plain positional index. A positional id
   would silently collide with a DIFFERENT logical candidate across runs
   once earlier items in the list are already done and skipped. At
   startup, each pass reads its own existing raw_<pass>.jsonl and treats
   every custom_id already present as DONE — already paid for, already
   has a result on disk, never resubmit it, regardless of whether it
   later passes Stage 5's grounding gate (the gate judges correctness of
   the final dataset, not whether generating the record was "worth it").

2. In-flight batch tracking (batch_state.json). A batch submitted before
   a crash/disconnect keeps running on Anthropic's side regardless of our
   own connection — it is real, already-billed work. The batch_id is
   persisted the moment submit_batch() returns, before polling even
   starts, so a restart reconnects to that same batch (poll + collect)
   instead of either losing the paid-for work or paying twice for a
   duplicate resubmission of the same candidates.
"""

import json
from pathlib import Path


def load_done_custom_ids(raw_path: Path) -> set[str]:
    """Every custom_id already durably written to this pass's raw jsonl.
    Missing/empty file (first-ever run, or a pass with nothing generated
    yet) simply yields an empty set — every candidate looks new."""
    if not raw_path.exists():
        return set()
    done: set[str] = set()
    with open(raw_path, encoding="utf-8") as source:
        for line in source:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            custom_id = record.get("custom_id")
            if custom_id:
                done.add(custom_id)
    return done


def load_all_instructions(raw_path: Path) -> list[str]:
    """Every `instruction` value already durably written to this pass's
    raw jsonl — used to rebuild Pass 3/4's question_pool from whatever is
    actually on disk (both prior-run and this-run records), rather than
    an in-memory list that a resumed, partially-skipped pass would build
    incompletely."""
    if not raw_path.exists():
        return []
    instructions: list[str] = []
    with open(raw_path, encoding="utf-8") as source:
        for line in source:
            line = line.strip()
            if not line:
                continue
            instructions.append(json.loads(line)["instruction"])
    return instructions


def load_batch_state(state_path: Path) -> dict:
    if not state_path.exists():
        return {}
    with open(state_path, encoding="utf-8") as source:
        return json.load(source)


def save_batch_state(state_path: Path, state: dict) -> None:
    with open(state_path, "w", encoding="utf-8") as dest:
        json.dump(state, dest, ensure_ascii=False, indent=2)


def record_batch_submitted(state_path: Path, pass_label: str, batch_id: str, items_meta: dict) -> None:
    """Called via execute_requests' on_batch_submitted hook the instant a
    batch is accepted — deliberately before wait_for_batch's poll loop
    starts, so a crash even one second into polling still has both the
    batch_id AND the per-item metadata needed to write results durably
    recorded. Without items_meta here, a resume could reconnect to the
    right batch but would have no idea what context/label/field_data each
    custom_id in it corresponds to, and so couldn't write real records
    from its results."""
    state = load_batch_state(state_path)
    state[pass_label] = {"batch_id": batch_id, "items_meta": items_meta}
    save_batch_state(state_path, state)


def clear_batch_state(state_path: Path, pass_label: str) -> None:
    """Called once a pass's batch (fresh or resumed) has been fully
    collected and written — a subsequent run must not try to reconnect to
    a batch that's already fully resolved."""
    state = load_batch_state(state_path)
    if pass_label in state:
        del state[pass_label]
        save_batch_state(state_path, state)


def wipe(raw_paths: dict, state_path: Path) -> None:
    """--fresh only: the one explicit, deliberate way to discard all
    prior progress and start completely over. Never called implicitly —
    the default behavior (no flag) always resumes."""
    for path in raw_paths.values():
        path.write_text("", encoding="utf-8")
    if state_path.exists():
        state_path.unlink()
