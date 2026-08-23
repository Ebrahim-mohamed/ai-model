#!/usr/bin/env python3
"""Admin CLI: Section 3 fine-tuning data-preparation pipeline (claude.md
§6.4 — adapt D:\\project\\project\\llm_finetuning.ipynb's teacher-
distillation methodology, don't redesign it). Runs the four sampling
passes (scripts/finetune_data/sampling.py) against real knowledge_chunks,
the Stage 5 deterministic grounding QA gate, and the Stage 6-8 Alpaca
formatting + stratified train/val split + dataset_info.json registration.

A one-shot admin script (claude.md §1.1) — lives under scripts/, never
imported by src/ at runtime, free to import FROM src/. Same
os.chdir(SRC_DIR)-before-importing-Settings() precedent this session's
own benchmark scripts already established, since
Settings.Config.env_file resolves relative to process cwd, not
config.py's own location.

LoRA fine-tuning itself is explicitly out of scope here — this script's
only job is producing scripts/finetune_data_out/{train,val}.json and
dataset_info.json. Requires ANTHROPIC_API_KEY in the environment (the
Anthropic SDK's own resolution — see teacher.py).

Cost note: a full run (no --limit) submits every pass through the
Anthropic Message Batches API — 50% off standard per-token pricing.
Real measured cost on the earlier one-call-at-a-time implementation was
exactly double the batch rate, confirming the switch. Each pass prints
its batch id and polls request_counts every 30s until done — a full run
is cheaper but takes longer wall-clock than the old live-call path,
since Anthropic schedules batches server-side (usually well under an
hour for a batch this size, but not instant). --limit runs stay on the
live, non-batch API on purpose, so a 2-row smoke test doesn't sit in
batch scheduling for that trade.

Resumability: every run (limited or full) is crash/disconnect/exhausted-
funds safe by default. At startup it reads whatever is already durably
written to raw_*.jsonl and reconnects to any batch left in-flight in
batch_state.json by a prior interruption (see finetune_data/checkpoint.py
and sampling.py's _run_checkpointed_batch) — a resumed run generates and
pays for only what's genuinely still missing, never re-pays for already-
billed work, and never silently starts over. Pass --fresh to explicitly
discard all prior progress and start clean.

Usage:
    python scripts/generate_finetuning_dataset.py --client-id raylab
    python scripts/generate_finetuning_dataset.py --client-id raylab            # safe to re-run after any interruption — resumes automatically
    python scripts/generate_finetuning_dataset.py --client-id raylab --limit 2  # cheap, fast smoke test (live API, not batched)
    python scripts/generate_finetuning_dataset.py --client-id raylab --fresh    # discard all prior progress, start over
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SRC_DIR = SCRIPT_DIR.parent / "src"
OUT_DIR = SCRIPT_DIR / "finetune_data_out"

os.chdir(SRC_DIR)
sys.path.insert(0, str(SRC_DIR))
sys.path.insert(0, str(SCRIPT_DIR))

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from helpers.config import get_settings  # noqa: E402
from models.ClientConfigModel import ClientConfigModel  # noqa: E402
from models.ChunkModel import ChunkModel  # noqa: E402
from controllers.RetrievalController import RetrievalController  # noqa: E402
from stores.vectordb.VectorDBProviderFactory import VectorDBProviderFactory  # noqa: E402
from stores.llm.LLMProviderFactory import LLMProviderFactory  # noqa: E402
from stores.reranker.RerankerProviderFactory import RerankerProviderFactory  # noqa: E402

from finetune_data import sampling, teacher, grounding_gate, format_alpaca, checkpoint, dedup  # noqa: E402
from finetune_data.cost_tracker import CostTracker  # noqa: E402


async def build_app_context() -> dict:
    """Mirrors main.py's startup_span() composition root — the same real
    providers, same warmup pattern (lazy-loaded model weights loaded here
    rather than on the first real call), constructed once for the whole
    run."""
    settings = get_settings()

    postgres_conn = (
        f"postgresql+asyncpg://{settings.POSTGRES_USERNAME}:{settings.POSTGRES_PASSWORD}"
        f"@{settings.POSTGRES_HOST}:{settings.POSTGRES_PORT}/{settings.POSTGRES_MAIN_DATABASE}"
    )
    db_engine = create_async_engine(postgres_conn, pool_pre_ping=True)
    db_client = sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

    client_config_model = await ClientConfigModel.create_instance(db_client)

    vectordb_provider_factory = VectorDBProviderFactory(config=settings, db_client=db_client)
    vectordb_client = vectordb_provider_factory.create(provider=settings.VECTOR_DB_BACKEND)
    chunk_model = await ChunkModel.create_instance(db_client, vectordb_client=vectordb_client)

    embedding_client = LLMProviderFactory(config=settings).create(provider=settings.EMBEDDING_BACKEND)
    reranker_client = RerankerProviderFactory(config=settings).create(provider=settings.RERANKER_BACKEND)
    await embedding_client.embed_text(["warmup"], is_query=False)
    await reranker_client.rerank("warmup", ["warmup"], top_k=1)

    retrieval_controller = RetrievalController(
        client_config_model=client_config_model, chunk_model=chunk_model,
        embedding_client=embedding_client, reranker_client=reranker_client,
    )

    return {
        "db_engine": db_engine,
        "client_config_model": client_config_model,
        "chunk_model": chunk_model,
        "retrieval_controller": retrieval_controller,
    }


async def run(client_id: str, limit: int | None = None, force_batch: bool = False, fresh: bool = False) -> None:
    if limit is not None:
        print(f"[test-mode] --limit {limit} — each pass capped to at most {limit} real teacher API call(s)")
    if force_batch:
        print("[test-mode] --force-batch — even the limited run will go through the real Batches API")
    OUT_DIR.mkdir(exist_ok=True)
    raw_paths = {
        "narrow_positive": OUT_DIR / "raw_narrow_positive.jsonl",
        "broad_positive": OUT_DIR / "raw_broad_positive.jsonl",
        "absence": OUT_DIR / "raw_absence.jsonl",
        "ambiguous": OUT_DIR / "raw_ambiguous.jsonl",
    }
    state_path = OUT_DIR / "batch_state.json"

    if fresh:
        print("[checkpoint] --fresh — wiping all raw_*.jsonl and batch_state.json, starting completely over")
        checkpoint.wipe(raw_paths, state_path)
    else:
        already_written = {label: len(checkpoint.load_done_custom_ids(path)) for label, path in raw_paths.items()}
        in_flight = checkpoint.load_batch_state(state_path)
        if any(already_written.values()) or in_flight:
            print("[checkpoint] resuming a prior run — existing progress found:")
            for label, count in already_written.items():
                if count:
                    print(f"    {label}: {count} records already on disk")
            for label in in_flight:
                print(f"    {label}: batch left in-flight by a prior crash/disconnect — will reconnect, not resubmit")
        else:
            print("[checkpoint] no prior progress found — starting fresh (pass --fresh to force this explicitly next time)")

    context = await build_app_context()
    try:
        client_config = await context["client_config_model"].get_client_config(client_id)
        if client_config is None:
            raise ValueError(f"No client_config row for client_id={client_id!r}")

        working_set = await sampling.load_working_set(context["chunk_model"], client_id)
        print(f"[stage0] loaded {len(working_set)} chunks with structured field_data for client_id={client_id!r}")
        if not working_set:
            raise ValueError(
                f"client_id={client_id!r} has zero chunks with field_data — nothing to generate from. "
                "Run a sync first (POST /api/sync)."
            )

        teacher_client = teacher.build_client()
        tracker = CostTracker()

        print("[stage1] Pass 1 — Narrow Positive...")
        narrow_questions = await sampling.run_pass1_narrow_positive(
            client=teacher_client, working_set=working_set,
            cost_tracker=tracker, out_path=raw_paths["narrow_positive"], state_path=state_path,
            limit=limit, force_batch=force_batch,
        )
        print(f"[stage1] {len(narrow_questions)} narrow-positive examples on disk (this run + any prior)")

        print("[stage2] Pass 2 — Broad Positive...")
        broad_questions = await sampling.run_pass2_broad_positive(
            client=teacher_client, working_set=working_set,
            retrieval_controller=context["retrieval_controller"], client_config=client_config,
            client_id=client_id, cost_tracker=tracker, out_path=raw_paths["broad_positive"], state_path=state_path,
            limit=limit, force_batch=force_batch,
        )
        print(f"[stage2] {len(broad_questions)} broad-positive examples on disk (this run + any prior)")

        question_pool = narrow_questions + broad_questions

        print("[stage3] Pass 3 — Absence...")
        await sampling.run_pass3_absence(
            client=teacher_client, working_set=working_set, question_pool=question_pool,
            cost_tracker=tracker, out_path=raw_paths["absence"], state_path=state_path,
            limit=limit, force_batch=force_batch,
        )

        print("[stage4] Pass 4 — Ambiguous...")
        await sampling.run_pass4_ambiguous(
            client=teacher_client, working_set=working_set,
            cost_tracker=tracker, out_path=raw_paths["ambiguous"], state_path=state_path,
            limit=limit, force_batch=force_batch,
        )

        tracker.final_report()

        print("[stage5] Grounding QA gate...")
        accepted, stats = grounding_gate.run_gate(list(raw_paths.values()), OUT_DIR / "rejected_ungrounded.jsonl")
        grounding_gate.print_gate_report(stats)

        if not accepted:
            print("[stage6-8] Zero records passed the grounding gate — nothing to write. "
                  "Check rejected_ungrounded.jsonl for why.")
            return

        print("[stage5.5] Deduplication...")
        accepted, dedup_stats = dedup.dedup_records(accepted)
        dedup.print_dedup_report(dedup_stats)

        print("[stage6-8] Formatting + stratified split + dataset_info.json...")
        train_count, val_count = format_alpaca.write_datasets(accepted, OUT_DIR)
        format_alpaca.register_dataset_info(OUT_DIR)
        print(
            f"[stage6-8] wrote train.json ({train_count} examples), val.json ({val_count} examples), "
            f"dataset_info.json — all under {OUT_DIR}"
        )

    finally:
        await context["db_engine"].dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the Section 3 fine-tuning dataset from real knowledge_chunks.")
    parser.add_argument("--client-id", required=True, help="Tenant to generate training data for (e.g. raylab)")
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Cap EACH pass to at most this many real teacher API calls (Pass 4's two sub-generators are each "
             "capped independently) — a cheap end-to-end smoke test before a full run. E.g. --limit 2. "
             "Omit entirely for a full run.",
    )
    parser.add_argument(
        "--force-batch", action="store_true",
        help="Only meaningful together with --limit: normally a limited run uses the live (non-batch) API for "
             "fast iteration. Pass this to route even a --limit run through the real Message Batches API instead "
             "— use it to cheaply prove the batch submit/poll/collect path works end-to-end on a couple of rows "
             "before trusting it on a full, expensive run.",
    )
    parser.add_argument(
        "--fresh", action="store_true",
        help="Wipe all raw_*.jsonl and batch_state.json and start completely over. The DEFAULT (no flag) always "
             "resumes: it reads whatever is already durably on disk and reconnects to any batch left in-flight by "
             "a prior crash/disconnect/exhausted-funds interruption, generating and paying for only what's still "
             "missing. Pass --fresh only when you deliberately want to discard prior progress (e.g. after a prompt "
             "or config change that invalidates it).",
    )
    args = parser.parse_args()
    asyncio.run(run(args.client_id, limit=args.limit, force_batch=args.force_batch, fresh=args.fresh))


if __name__ == "__main__":
    main()
