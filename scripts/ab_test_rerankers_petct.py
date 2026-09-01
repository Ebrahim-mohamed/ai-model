#!/usr/bin/env python3
"""Admin CLI: A/B comparison of CrossEncoderProvider (mmarco-mMiniLMv2,
the deployed default) vs. BGERerankerV2M3Provider (BAAI/bge-reranker-v2-m3,
the new togglable candidate — RerankerEnums.BGE_RERANKER_V2_M3) on the
exact real query that surfaced the lexical-overlap failure: the reranker
scoring a "غرفة PET CT" (PET-CT room) branch-logistics chunk above the
actual PET-CT exam/procedure chunk.

Both rerankers score the IDENTICAL candidate set — the real, live
hybrid_search (dense+sparse+RRF) fused pool for this query, fetched once
via the same real ChunkModel/VectorDBProvider path RetrievalController
itself uses — so the only variable between the two columns is the
reranker model itself, nothing upstream.

This is a debugging/evaluation aid, not a Verification/Test pass
(claude.md §4.3 draws that line explicitly) — it calls chunk_model.
hybrid_search directly rather than through the real /api/whatsapp/chat
HTTP endpoint, on purpose, since the question here is specifically "how
do these two rerankers each score the same real candidates," not an
end-to-end endpoint check.

A one-shot admin script (claude.md §1.1) — lives under scripts/, never
imported by src/ at runtime. Mirrors investigate_retrieval_task1.py's own
build_app_context() wiring pattern (same real providers, same
os.chdir-before-importing-Settings precedent).

Usage:
    python scripts/ab_test_rerankers_petct.py --client-id raylab
    python scripts/ab_test_rerankers_petct.py --client-id raylab --query "..."
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
from stores.vectordb.VectorDBProviderFactory import VectorDBProviderFactory  # noqa: E402
from stores.llm.LLMProviderFactory import LLMProviderFactory  # noqa: E402
from stores.reranker.providers.CrossEncoderProvider import CrossEncoderProvider, MODEL_NAME as CROSS_ENCODER_MODEL_NAME  # noqa: E402
from stores.reranker.providers.BGERerankerV2M3Provider import BGERerankerV2M3Provider, MODEL_NAME as BGE_MODEL_NAME  # noqa: E402

# The exact real query from this session's live Postman traffic that
# surfaced the lexical-overlap failure (Turn 6 of the 7-turn stress test).
DEFAULT_QUERY = "بالنسبة لفحص PET-CT، أنا تبع كايروسكان وهدفع كاش، إيه الإجراءات؟"


async def build_app_context():
    """Same real wiring as main.py's startup_span() / investigate_retrieval_task1.py's
    build_app_context() — real DB, real embedding client, real chunk_model.
    Deliberately does NOT build a reranker_client here (unlike those
    scripts) — this script instantiates both reranker providers directly,
    side by side, instead of going through RerankerProviderFactory's
    single-backend selection."""
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
    await embedding_client.embed_text(["warmup"], is_query=False)

    return chunk_model, embedding_client, client_config_model, db_engine


def _snippet(text: str, length: int = 90) -> str:
    text = text.replace("\n", " ")
    return text if len(text) <= length else text[:length] + "…"


async def run(client_id: str, query: str) -> None:
    chunk_model, embedding_client, client_config_model, db_engine = await build_app_context()
    try:
        client_config = await client_config_model.get_client_config(client_id)
        top_k = client_config.whatsapp_retrieval_top_k_broad
        rrf_k = client_config.rrf_k
        candidate_k = max(20, top_k * 4)

        print("=" * 90)
        print(f"QUERY: {query}")
        print(f"candidate_k={candidate_k}  rrf_k={rrf_k}  top_k={top_k}")
        print("=" * 90)

        # Real, live hybrid_search (dense+sparse+RRF) — the exact same
        # fused candidate pool RetrievalController.retrieve() would hand
        # to a single reranker. Fetched ONCE so both rerankers below
        # score the identical documents.
        query_vectors = await embedding_client.embed_text([query], is_query=True)
        query_vector = query_vectors[0]

        fused_chunks = await chunk_model.hybrid_search(
            client_id=client_id,
            query_text=query,
            query_vector=query_vector,
            candidate_k=candidate_k,
            rrf_k=rrf_k,
            top_k=candidate_k,
            metadata_filters=None,
        )

        if not fused_chunks:
            print("ZERO chunks from hybrid_search. Nothing to rerank.")
            return

        documents = [chunk.content for chunk in fused_chunks]
        print(f"\nhybrid_search returned {len(documents)} candidates. Reranking with both models...\n")

        cross_encoder = CrossEncoderProvider()
        bge_reranker = BGERerankerV2M3Provider()

        print(f"Loading {CROSS_ENCODER_MODEL_NAME} ...")
        cross_encoder_results = await cross_encoder.rerank(query=query, documents=documents, top_k=len(documents))
        print(f"Loading {BGE_MODEL_NAME} (larger model — first load takes longer) ...")
        bge_results = await bge_reranker.rerank(query=query, documents=documents, top_k=len(documents))

        cross_encoder_rank_of = {index: rank for rank, (index, _score) in enumerate(cross_encoder_results, start=1)}
        bge_rank_of = {index: rank for rank, (index, _score) in enumerate(bge_results, start=1)}
        cross_encoder_score_of = dict(cross_encoder_results)
        bge_score_of = dict(bge_results)

        print("\n" + "=" * 90)
        print(f"{'#':<4}{'CrossEncoder (mmarco-mMiniLMv2)':<38}{'BGE-Reranker-v2-M3':<38}Chunk")
        print("-" * 90)
        for original_index, chunk in enumerate(fused_chunks):
            metadata = chunk.metadata_payload or {}
            label = f"sheet={metadata.get('sheet_name')!r}  {_snippet(chunk.content)}"
            ce_rank = cross_encoder_rank_of.get(original_index)
            ce_score = cross_encoder_score_of.get(original_index)
            bge_rank = bge_rank_of.get(original_index)
            bge_score = bge_score_of.get(original_index)
            ce_col = f"rank={ce_rank:<3} score={ce_score:+.4f}" if ce_rank else "—"
            bge_col = f"rank={bge_rank:<3} score={bge_score:+.4f}" if bge_rank else "—"
            print(f"{original_index:<4}{ce_col:<38}{bge_col:<38}{label}")
        print("=" * 90)
        print("\nCompare which chunk each model ranked #1 above — that's the real, direct "
              "answer to whether BGE-Reranker-v2-M3 avoids the lexical-overlap trap.")
    finally:
        await db_engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-id", default="raylab")
    parser.add_argument("--query", default=DEFAULT_QUERY)
    args = parser.parse_args()
    asyncio.run(run(client_id=args.client_id, query=args.query))
