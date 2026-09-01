#!/usr/bin/env python3
"""Admin CLI: retrieval diagnostic, v2 — separating the "wrapped-narrow-
context noise" theory from the "HNSW ranking non-determinism" theory for
the golden-suite v2 regressions (cases 24/56/69: previously correct,
single-chunk narrow answers that started producing completely empty
debug_json once whatsapp_retrieval_top_k_narrow went 1 -> 3), alongside
the three original under-confidence cases (9/50/60, never correctly
extracted at any config).

For each case this shows, side by side:
  - the REAL breadth classification (_classify_breadth, the exact live
    method — not reimplemented) this query gets against the current
    broad-ceiling candidate set,
  - the narrow-window slice (top whatsapp_retrieval_top_k_narrow chunks)
    that would actually reach the model if breadth classifies narrow,
  - the full broad-ceiling set (top whatsapp_retrieval_top_k_broad) that
    the widening retry would fall back to.

How to read the output for cases 24/56/69:
  - If the expected fact's field/value is visibly sitting inside the
    NARROW WINDOW's own field_data (i.e. retrieval found it, at the
    narrow ceiling, same as before) -- that's evidence FOR the noise/
    format theory: the fact was retrieved, but the wrapped multi-chunk
    CONTEXT (a shape the fine-tuned model never trained on for narrow
    breadth) or the extra chunks' noise kept the model from extracting
    it.
  - If the expected fact's field/value is NOT visibly present anywhere
    in either window (narrow or broad) -- that's evidence FOR the
    ranking/non-determinism theory: this specific run's retrieval
    genuinely didn't surface the right chunk at all, independent of
    narrow-vs-broad or wrapping.

This is a debugging aid, not a Verification/Test pass (claude.md §4.3
draws that line explicitly) — it calls RetrievalController directly
rather than through the real /api/whatsapp/chat HTTP endpoint, on
purpose, since the question here is specifically "what did retrieval
actually return," not an end-to-end endpoint check.

A one-shot admin script (claude.md §1.1) — lives under scripts/, never
imported by src/ at runtime. Mirrors generate_finetuning_dataset.py's own
build_app_context() wiring pattern (same real providers, same os.chdir-
before-importing-Settings precedent) without pulling in that script's
unrelated teacher-distillation imports.

Usage:
    python scripts/investigate_retrieval_task1.py --client-id raylab
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SRC_DIR = SCRIPT_DIR.parent / "src"

os.chdir(SRC_DIR)
sys.path.insert(0, str(SRC_DIR))

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from helpers.config import get_settings  # noqa: E402
from models.ClientConfigModel import ClientConfigModel  # noqa: E402
from models.ChunkModel import ChunkModel  # noqa: E402
from controllers.RetrievalController import RetrievalController  # noqa: E402
from controllers.TextReplyController import TextReplyController  # noqa: E402
from stores.vectordb.VectorDBProviderFactory import VectorDBProviderFactory  # noqa: E402
from stores.llm.LLMProviderFactory import LLMProviderFactory  # noqa: E402
from stores.reranker.RerankerProviderFactory import RerankerProviderFactory  # noqa: E402


CASES = [
    # Original under-confidence cases (never correctly extracted at any
    # top_k config so far — likely a genuine ranking miss, not a
    # narrow/broad or wrapping artifact).
    {
        "index": 9,
        "query": "لو عايز اضيف رسم عصب بالإبرة على رسم العصب العادي، هياخد فلوس زياده؟",
        "expected_fact": "اذا طلب العميل رسم عصب بالابرة يتم اضافة 500 جنية علي سعر الفحص "
                          "واختيار خدمات اضافية خلال الحجز واختيار EMG Needle",
    },
    {
        "index": 50,
        "query": "خدمة التخدير الكلي متاحة في فرع الحوامدية؟",
        "expected_fact": "الـفـروع المتاح بـهــا تـخـديــر: الحوامديه. Accounts: تكنوسكان",
    },
    {
        "index": 60,
        "query": "ممكن اخويا ياخد فلوس الكاش باك بدالي؟",
        "expected_fact": "البند: طريقة الاستلام. الشرط: العميل بنفسه + بطاقة شخصية + إيصال",
    },
    # New regressions (2026-08-26): correctly answered with a single
    # clean chunk before whatsapp_retrieval_top_k_narrow was raised 1->3;
    # now produce completely empty debug_json at both the narrow (3) and
    # widened-retry (5) window.
    {
        "index": 24,
        "query": "باقة الفحص الشامل للسيدات سعرها كام؟",
        "expected_fact": "السعر الكلي للباقة: 2000",
    },
    {
        "index": 56,
        "query": "موافقة عناية مصر مدتها كام يوم؟",
        "expected_fact": "الشركة: عناية مصر. مده الصلاحية: 7 ايام",
    },
    {
        "index": 69,
        "query": "نسبة كاش باك كارت لاكي على التحاليل قد ايه بالظبط؟",
        "expected_fact": "تبلغ نسبة الاسترداد 25% على الفحوصات الإشعاعية و15% على التحاليل المعملية",
    },
]


async def build_app_context():
    """Same real wiring as main.py's startup_span() / generate_finetuning_dataset.py's
    build_app_context() — same providers, same warmup pattern."""
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
    # Only used for its _classify_breadth method below (real logic, not
    # reimplemented) -- the other four collaborators are never touched by
    # that method, so None is safe here.
    text_reply_controller = TextReplyController(None, None, None, None, None, None)

    return retrieval_controller, text_reply_controller, client_config_model, db_engine


def _print_window(label, results):
    if not results:
        print(f"  {label}: ZERO chunks.")
        return
    print(f"  {label} ({len(results)} chunks):")
    for rank, result in enumerate(results, start=1):
        chunk = result["chunk"]
        metadata = chunk.metadata_payload or {}
        field_data = metadata.get("field_data", {})
        print(f"    #{rank}  score={result['score']:.4f}  sheet={metadata.get('sheet_name')!r}  "
              f"source_file={metadata.get('source_file')!r}")
        print(f"         field_data={field_data}")


async def run(client_id: str) -> None:
    retrieval_controller, text_reply_controller, client_config_model, db_engine = await build_app_context()
    try:
        client_config = await client_config_model.get_client_config(client_id)
        broad_top_k = client_config.whatsapp_retrieval_top_k_broad
        narrow_top_k = client_config.whatsapp_retrieval_top_k_narrow
        print(f"client_config: whatsapp_retrieval_top_k_narrow={narrow_top_k}  "
              f"whatsapp_retrieval_top_k_broad={broad_top_k}")

        for case in CASES:
            print("\n" + "=" * 78)
            print(f"[{case['index']}] QUERY: {case['query']}")
            print(f"EXPECTED FACT: {case['expected_fact']}")
            print("-" * 78)

            # Exact same real broad-ceiling call TextReplyController._mode_a_reply
            # makes first, unconditionally, before breadth classification.
            all_results = await retrieval_controller.retrieve(
                client_id=client_id, query=case["query"], top_k_override=broad_top_k,
            )

            if not all_results:
                print("  ZERO chunks retrieved at the broad ceiling. Nothing to slice.")
                continue

            # Real classification, not reimplemented -- exactly what this
            # query would get routed as in production right now.
            breadth = text_reply_controller._classify_breadth(case["query"], all_results, client_config)
            print(f"  breadth classification: {breadth!r}")

            narrow_window = all_results[:narrow_top_k] if breadth == "narrow" else all_results
            _print_window(
                f"NARROW WINDOW (top {narrow_top_k}, what the first generation call would see)"
                if breadth == "narrow" else "BROAD WINDOW (breadth classified broad, no narrow slice)",
                narrow_window,
            )
            if breadth == "narrow" and len(all_results) > len(narrow_window):
                _print_window(
                    f"FULL BROAD-CEILING SET (top {broad_top_k}, what the widening retry would see)",
                    all_results,
                )
    finally:
        await db_engine.dispose()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-id", required=True, dest="client_id")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(run(args.client_id))
