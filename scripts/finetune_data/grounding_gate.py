"""Stage 5 — the deterministic, non-LLM training-data QA gate. Verifies
every distilled record's JSON block and phrasing are grounded in the
REAL structured field_data captured by sampling.py at generation time
(the `grounding` block on each raw record) — never by re-parsing
ChunkingController's flattened, period-joined `content` string back into
a dict, which is fragile and unnecessary when the structured source is
already on hand (see sampling.py's module docstring).

This gate is allowed to be stricter than the live Layer 1/2/3
verification pipeline this session built earlier: a false rejection here
just discards one training example (regenerate and move on), it doesn't
escalate a real patient conversation unnecessarily the way an
over-eager live gate would. So it leans on the one signal this session's
own benchmarks actually proved has zero false positives standalone —
deterministic exact-match / numeric grounding, Layer 1's core mechanism
— as the sole auto-reject criterion. It deliberately does NOT reuse
Layer 2's embedding-similarity field matching or Layer 3's NLI classifier
as auto-reject signals: this session's own benchmarks measured those at
~43% and ~52% false-positive rates respectively, so trusting either to
silently discard good training data would waste far more than it
protects. Non-numeric claim drift is a real, disclosed gap — this gate
only auto-rejects a numeric fabrication, and does not further check
non-numeric phrasing claims against the JSON (a candidate soft/manual-
review signal for a later iteration, not implemented here).
"""

import json
import re
from pathlib import Path

NUMBER_TOKEN_RE = re.compile(r"\d+(?:[:.,]\d+)*")


def _normalize(value) -> str:
    return str(value).strip()


def _all_values_text(field_data: dict) -> str:
    return " ".join(_normalize(v) for v in field_data.values())


class GateResult:
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


def _check_keys_and_values(output_json: dict, field_data: dict) -> GateResult:
    if not isinstance(output_json, dict):
        return GateResult.reject("output_json_not_a_dict", got=type(output_json).__name__)
    for key, value in output_json.items():
        if key not in field_data:
            return GateResult.reject("invented_key", key=key)
        if _normalize(value) != _normalize(field_data[key]):
            return GateResult.reject(
                "value_mismatch", key=key, expected=_normalize(field_data[key]), got=_normalize(value),
            )
    return GateResult.accept()


def _check_phrasing_numeric_grounding(phrasing: str, allowed_values_text: str) -> GateResult:
    """Hard auto-reject tier only: every number appearing in a claim
    sentence must appear among the allowed values' own numbers — not just
    somewhere in the wider source context, and never in a sentence ending
    in a question mark (a scripted follow-up to the patient, not a claim
    about a fact — same non-claim-sentence principle Layer 2 established
    this session)."""
    allowed_numbers = set(NUMBER_TOKEN_RE.findall(allowed_values_text))
    for sentence in re.split(r"(?<=[.!؟?])\s+", phrasing):
        stripped = sentence.strip()
        if not stripped or stripped.endswith(("؟", "?")):
            continue
        for number in NUMBER_TOKEN_RE.findall(stripped):
            if number not in allowed_numbers:
                return GateResult.reject("unverified_numeric_claim", sentence=stripped, number=number)
    return GateResult.accept()


def check_record(record: dict) -> GateResult:
    grounding = record.get("grounding", {})
    kind = grounding.get("type")
    output_json = record.get("output_json")
    phrasing = record.get("output_phrasing", "")

    if kind == "narrow":
        field_data = grounding["field_data"]
        result = _check_keys_and_values(output_json, field_data)
        if not result.accepted:
            return result
        return _check_phrasing_numeric_grounding(phrasing, _all_values_text(field_data))

    if kind == "broad":
        sources_field_data = grounding["sources_field_data"]
        if not isinstance(output_json, list):
            return GateResult.reject("broad_output_not_a_list", got=type(output_json).__name__)

        declared = {entry.get("source") for entry in output_json}
        expected = set(range(1, len(sources_field_data) + 1))
        if declared != expected:
            return GateResult.reject("source_count_mismatch", declared=sorted(declared), expected=sorted(expected))

        all_values_text = ""
        for entry in output_json:
            source_index = entry["source"]
            source_field_data = sources_field_data[source_index - 1]
            # Checked per-source, against ONLY that source's own
            # field_data — this is the exact check that catches the real,
            # documented cross-chunk misattribution bug the
            # [BEGIN SOURCE n] wrapper was built to fix. A value that's
            # genuinely grounded elsewhere in the overall context but
            # attributed to the wrong source fails here.
            result = _check_keys_and_values(entry.get("fields", {}), source_field_data)
            if not result.accepted:
                return GateResult.reject("wrong_source_attribution", source=source_index, **result.detail)
            all_values_text += " " + _all_values_text(source_field_data)

        return _check_phrasing_numeric_grounding(phrasing, all_values_text)

    if kind == "absence":
        if output_json not in ({}, []):
            return GateResult.reject("non_empty_json_for_absence", got=output_json)
        return _check_phrasing_numeric_grounding(phrasing, "")

    if kind == "ambiguous_direct":
        target_field = grounding["target_field"]
        if isinstance(output_json, dict) and _normalize(output_json.get(target_field, "")) != "":
            return GateResult.reject("invented_value_for_missing_field", key=target_field)
        return _check_phrasing_numeric_grounding(phrasing, "")

    if kind == "ambiguous_cross_chunk":
        if output_json not in ({}, []):
            return GateResult.reject("non_empty_json_for_ambiguous", got=output_json)
        values = grounding.get("conflicting_values", [])
        if len(set(values)) < 2:
            # Stage 4(b)'s own conflict detection could false-positive on
            # formatting noise (e.g. whitespace) — re-confirmed here
            # against the values actually recorded at generation time.
            return GateResult.reject("ambiguous_premise_invalid", values=values)
        return _check_phrasing_numeric_grounding(phrasing, "")

    return GateResult.reject("unknown_grounding_type", kind=kind)


def run_gate(raw_paths: list[Path], rejected_path: Path) -> tuple[list[dict], dict]:
    accepted: list[dict] = []
    stats: dict[str, dict[str, int]] = {}

    with open(rejected_path, "w", encoding="utf-8") as rejected_dest:
        for raw_path in raw_paths:
            if not raw_path.exists():
                continue
            with open(raw_path, encoding="utf-8") as src:
                for line in src:
                    line = line.strip()
                    if not line:
                        continue
                    record = json.loads(line)
                    pass_label = record.get("pass", "unknown")
                    pass_stats = stats.setdefault(pass_label, {"generated": 0, "accepted": 0, "rejected": 0})
                    pass_stats["generated"] += 1

                    result = check_record(record)
                    if result.accepted:
                        pass_stats["accepted"] += 1
                        accepted.append(record)
                    else:
                        pass_stats["rejected"] += 1
                        rejected_dest.write(json.dumps({
                            "record": record,
                            "pass": pass_label,
                            "rejection_stage": "structural" if result.reason in (
                                "output_json_not_a_dict", "broad_output_not_a_list",
                            ) else "grounding",
                            "rejection_reason": result.reason,
                            "detail": result.detail,
                        }, ensure_ascii=False) + "\n")

    return accepted, stats


def print_gate_report(stats: dict) -> None:
    print("\n" + "=" * 64)
    print("Stage 5 — grounding QA gate report")
    print("=" * 64)
    for pass_label, counts in stats.items():
        rate = (counts["accepted"] / counts["generated"] * 100) if counts["generated"] else 0.0
        print(
            f"  {pass_label:20s} generated={counts['generated']:4d}  "
            f"accepted={counts['accepted']:4d}  rejected={counts['rejected']:4d}  "
            f"pass_rate={rate:.1f}%"
        )
    print("=" * 64 + "\n")
