#!/usr/bin/env python3
"""Admin CLI: Task 1 diagnostic (follow-up to the golden-suite v2 audit) —
runs a handful of real patient queries directly through
RetrievalController.retrieve() (the exact real call TextReplyController
makes for a Mode A turn's broad-ceiling retrieval, before breadth
classification narrows it down) and prints every returned chunk's score,
sheet, source file, and full field_data, so it's possible to see directly
whether the fact the model declined to answer with was ever actually
retrieved at all.

This is a debugging aid, not a Verification/Test pass (claude.md §4.3
draws that line explicitly) — it calls RetrievalController directly rather
than through the real /api/whatsapp/chat HTTP endpoint, on purpose, since
the question here is specifically "did retrieval find the right chunk,"
not an end-to-end endpoint check.

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
from stores.vectordb.VectorDBProviderFactory import VectorDBProviderFactory  # noqa: E402
from stores.llm.LLMProviderFactory import LLMProviderFactory  # noqa: E402
from stores.reranker.RerankerProviderFactory import RerankerProviderFactory  # noqa: E402


# The three real cases the golden-suite v2 audit flagged: model declined
# ("المعلومة دي مش متوفرة عندي") with an empty debug_json, on a fact that
# the same source sheet answered correctly elsewhere in the same run.
CASES = [
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
]


async def build_retrieval_controller():
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
    return retrieval_controller, client_config_model, db_engine


async def run(client_id: str) -> None:
    retrieval_controller, client_config_model, db_engine = await build_retrieval_controller()
    try:
        client_config = await client_config_model.get_client_config(client_id)
        # Same broad-ceiling call TextReplyController._mode_a_reply makes,
        # BEFORE breadth classification narrows it down -- the real first
        # retrieval pass every Mode A turn goes through.
        broad_top_k = client_config.whatsapp_retrieval_top_k_broad

        for case in CASES:
            print("\n" + "=" * 78)
            print(f"[{case['index']}] QUERY: {case['query']}")
            print(f"EXPECTED FACT: {case['expected_fact']}")
            print("-" * 78)

            results = await retrieval_controller.retrieve(
                client_id=client_id, query=case["query"], top_k_override=broad_top_k,
            )

            if not results:
                print("  ZERO chunks retrieved.")
                continue

            for rank, result in enumerate(results, start=1):
                chunk = result["chunk"]
                metadata = chunk.metadata_payload or {}
                field_data = metadata.get("field_data", {})
                print(f"  #{rank}  score={result['score']:.4f}  sheet={metadata.get('sheet_name')!r}  "
                      f"source_file={metadata.get('source_file')!r}")
                print(f"       field_data={field_data}")
    finally:
        await db_engine.dispose()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-id", required=True, dest="client_id")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(run(args.client_id))
