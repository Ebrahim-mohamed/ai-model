"""Stages 6-8 — reformats gate-passed raw records into the final Alpaca
{system, instruction, input, output, history} shape, mirroring
llm_finetuning.ipynb's separate "Format Finetuning Datasets" cell exactly
(a distinct pass over the already-generated raw data, never folded into
the distillation loop itself). Then a fixed-seed shuffle and a
per-pass-stratified train/val split — a deliberate, disclosed deviation
from the notebook's blind `data[:N]`/`data[N:]` slice, justified because
our dataset is four passes with very different sizes and difficulty, not
one homogeneous news corpus; a blind slice risks skewing val.json toward
whichever pass happened to sort last."""

import json
import random
from pathlib import Path

from stores.llm.templates.template_parser import TemplateParser, TemplateBucket

FIXED_SEED = 101
VAL_FRACTION = 0.05  # small held-out slice per pass — comparable in spirit to the notebook's ~2.4% (66/2766)
MIN_RECORDS_FOR_VAL_SPLIT = 20  # a pass smaller than this contributes everything to train, not a token to val


def format_record(record: dict) -> dict:
    system_prompt = TemplateParser().resolve(TemplateBucket.C, "whatsapp_mode_a_reply_directive")
    output_json_text = json.dumps(record["output_json"], ensure_ascii=False)
    output = f"```json\n{output_json_text}\n```\n\n{record['output_phrasing']}"

    return {
        "system": system_prompt,
        "instruction": record["instruction"],
        "input": record["input"],
        "output": output,
        "history": [],
    }


def split_stratified(accepted_records: list[dict]) -> tuple[list[dict], list[dict]]:
    """Operates on the RAW accepted records (still carrying `pass`) —
    stratification must happen before format_record() strips that key."""
    by_pass: dict[str, list[dict]] = {}
    for record in accepted_records:
        by_pass.setdefault(record.get("pass", "unknown"), []).append(record)

    rng = random.Random(FIXED_SEED)
    train_raw: list[dict] = []
    val_raw: list[dict] = []
    for records in by_pass.values():
        shuffled = list(records)
        rng.shuffle(shuffled)
        val_count = round(len(shuffled) * VAL_FRACTION) if len(shuffled) >= MIN_RECORDS_FOR_VAL_SPLIT else 0
        val_raw.extend(shuffled[:val_count])
        train_raw.extend(shuffled[val_count:])

    rng.shuffle(train_raw)
    rng.shuffle(val_raw)
    return train_raw, val_raw


def write_datasets(accepted_records: list[dict], out_dir: Path) -> tuple[int, int]:
    train_raw, val_raw = split_stratified(accepted_records)
    train = [format_record(r) for r in train_raw]
    val = [format_record(r) for r in val_raw]

    with open(out_dir / "train.json", "w", encoding="utf-8") as f:
        json.dump(train, f, ensure_ascii=False)
    with open(out_dir / "val.json", "w", encoding="utf-8") as f:
        json.dump(val, f, ensure_ascii=False)

    return len(train), len(val)


def register_dataset_info(out_dir: Path) -> None:
    """Writes/updates dataset_info.json with the standard LLaMA-Factory
    column mapping (prompt/query/response/system/history) for both
    splits — the notebook's own mapping (its "Configure LLaMA-Factory"
    cell), written to a file here instead of a manual copy-paste step."""
    dataset_info_path = out_dir / "dataset_info.json"
    existing = {}
    if dataset_info_path.exists():
        with open(dataset_info_path, encoding="utf-8") as f:
            existing = json.load(f)

    column_mapping = {
        "prompt": "instruction",
        "query": "input",
        "response": "output",
        "system": "system",
        "history": "history",
    }
    existing["raylab_finetune_train"] = {
        "file_name": str((out_dir / "train.json").resolve()),
        "columns": column_mapping,
    }
    existing["raylab_finetune_val"] = {
        "file_name": str((out_dir / "val.json").resolve()),
        "columns": column_mapping,
    }

    with open(dataset_info_path, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)
