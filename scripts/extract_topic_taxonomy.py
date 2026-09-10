#!/usr/bin/env python3
"""Admin CLI: extract a starting client_config.topic_taxonomy from the real
knowledge_chunks catalog, and (optionally) write it.

Why this exists (2026-09-08, Analytics Dashboard pipeline): topic_taxonomy
is a client_config JSONB column — QueryRouterInterface.classify_topic's
closed-set vocabulary — with the same admin-curated, starts-empty bootstrap
story as client_config.brand_value_aliases. Typing a large exam/lab-test
catalog into psql by hand (a raw `UPDATE ... SET topic_taxonomy = '[...]'`)
is exactly the failure mode onboard_client.py's own docstring already
documents and moved away from for onedrive_item_id/admin_api_key: shell
quoting/history-expansion around a hand-typed value is error-prone, and
a JSON array of dozens of real exam names is a much larger surface for
that same mistake than a single string ever was. This script writes
through ClientConfigModel.upsert_client_config (the same repository method
onboard_client.py already uses), never raw SQL — client_config's own
generic **fields upsert already supports this with zero model changes.

Scope and the real key-naming problem this works around: ChunkingController
never uses a fixed field-name schema (claude.md §1.3 — columns are
discovered per-sheet from schema_registry, never hardcoded), so there is no
single reliable "the exam name field" key across the whole corpus. Real,
verified-against-live-data examples: the Examinations/الفحوصات sheets use
"اسم الفحص", the تحاليل (lab tests) sheet uses "اسم التحليل" instead — two
different literal keys for the same underlying concept. Rather than
hardcoding both exact strings (which would silently miss a third sheet
using yet another spelling — the identical mistake brand_value_aliases'
own predecessor, metadata_promotion_map, already made and was replaced
over), this script matches any field_data key that STARTS WITH "اسم"
(Arabic for "name") once ARABIC TATWEEL (U+0640, the source Excel's own
calligraphic header-stretching character — see TextReplyController.
_TATWEEL_RE's own comment for the same real, confirmed artifact) is
stripped from the key first. Real chunk VALUES are never touched here,
matching that same file's own "prompt-build-time-only, never touches what
was stored" discipline.

Scoped to exactly three real, verified chunk_type values that hold
patient-facing offerings (not administrative/policy sheets like Branch
Directory, Insurance Guide, or Weights): "Examinations", "الفحوصات",
"تحاليل" (each compared post-strip() — the live data carries inconsistent
trailing whitespace on chunk_type, confirmed directly against this
project's own real corpus, not assumed).

Known, disclosed limitation (read before trusting --apply blindly): this
is a starting SCAFFOLD, not a finished taxonomy. It does no deduplication
across language/phrasing variants of the same real exam (e.g. an English
and an Arabic name for the same test can both survive as separate
entries), and doesn't merge near-duplicates. Review the printed list
before applying — same "starting point pending real calibration" status
every other config value in this project carries, not a finished admin
tool.

Usage:
    python scripts/extract_topic_taxonomy.py --client-id raylab
    python scripts/extract_topic_taxonomy.py --client-id raylab --apply

Runs from anywhere — it locates src/ relative to its own file location,
mirroring onboard_client.py/investigate_retrieval_task1.py's own
os.chdir-before-importing-Settings precedent.
"""

import argparse
import asyncio
import os
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SRC_DIR = SCRIPT_DIR.parent / "src"
os.chdir(SRC_DIR)
sys.path.insert(0, str(SRC_DIR))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from helpers.config import get_settings  # noqa: E402
from models.ClientConfigModel import ClientConfigModel  # noqa: E402
from models.db_schemes.raylab.schemes import KnowledgeChunk  # noqa: E402

# Same artifact TextReplyController._TATWEEL_RE strips from chunk CONTENT
# at prompt-build time — here applied to field_data KEYS instead, so the
# same real label ("اسم الفحص") matches regardless of how many tatweel
# characters the source Excel's header styling happened to insert on a
# given row.
_TATWEEL_RE = re.compile("ـ+")
# باقات التحاليل (2026-09-08 correction) added after the first --apply run
# was found to have missed it: it genuinely has its own "اسم الباقه ..."
# name field, confirmed against real chunk content, same shape as the
# other three -- Insurance Guide/Branch Directory/Weights/Exam
# Preparation/Sheet1 and the rest were individually checked against real
# content too and confirmed NOT to be offering-shaped (Sheet1 in
# particular turned out to be a doctor-specialization directory keyed by
# doctor name, not an exam/service list).
_OFFERING_CHUNK_TYPES = {"Examinations", "الفحوصات", "تحاليل", "باقات التحاليل"}
_NAME_PREFIX = "اسم"  # "name" -- matches "اسم الفحص"/"اسم التحليل"/"اسم الباقه" alike


def _normalize(text: str) -> str:
    return _TATWEEL_RE.sub("", text).strip()


def _find_name_fields(field_data: dict) -> list[str]:
    """Returns EVERY value whose key starts with 'اسم', not just the
    first — 2026-09-08 correction: Examinations/تحاليل rows usually carry
    one combined bilingual name in a single field, but باقات التحاليل
    splits Arabic/English into two separate 'اسم الباقه ...' fields —
    taking only the first match would silently drop the English package
    name."""
    names = []
    for key, value in field_data.items():
        if _normalize(key).startswith(_NAME_PREFIX) and value and str(value).strip():
            # Real values sometimes carry a soft line-wrap within the
            # source cell -- collapse whitespace only, never touch the
            # real characters (same non-destructive spirit as
            # TextReplyController._normalize_context_text).
            names.append(re.sub(r"\s+", " ", str(value)).strip())
    return names


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract a starting topic_taxonomy for client_config from the "
                    "real Examinations/الفحوصات/تحاليل catalog's own name fields.",
    )
    parser.add_argument("--client-id", required=True, dest="client_id")
    parser.add_argument(
        "--apply", action="store_true",
        help="Write the extracted list to client_config.topic_taxonomy. Without this "
             "flag, the list is only printed for review -- nothing is written.",
    )
    args = parser.parse_args()

    settings = get_settings()
    conn = (
        f"postgresql+asyncpg://{settings.POSTGRES_USERNAME}:{settings.POSTGRES_PASSWORD}"
        f"@{settings.POSTGRES_HOST}:{settings.POSTGRES_PORT}/{settings.POSTGRES_MAIN_DATABASE}"
    )
    engine = create_async_engine(conn, pool_pre_ping=True)
    db_client = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    try:
        async with db_client() as session:
            stmt = select(KnowledgeChunk).where(KnowledgeChunk.client_id == args.client_id)
            result = await session.execute(stmt)
            all_chunks = result.scalars().all()

        chunks = [c for c in all_chunks if (c.chunk_type or "").strip() in _OFFERING_CHUNK_TYPES]
        if not chunks:
            print(f"No offering-shaped chunks found for client_id={args.client_id!r}.")
            return 1

        names: set[str] = set()
        unmatched = 0
        for chunk in chunks:
            field_data = (chunk.metadata_payload or {}).get("field_data") or {}
            matches = _find_name_fields(field_data)
            if matches:
                names.update(matches)
            else:
                unmatched += 1

        taxonomy = sorted(names)

        print(f"Scanned {len(chunks)} offering chunks for client_id={args.client_id!r} "
              f"(of {len(all_chunks)} total chunks).")
        print(f"Extracted {len(taxonomy)} distinct name values ({unmatched} chunks had no matching field).\n")
        for name in taxonomy:
            print(f"  - {name}")

        print(
            "\nKNOWN LIMITATION: starting scaffold, not a finished taxonomy -- the same "
            "real exam/test can appear as separate near-duplicate entries in different "
            "languages or phrasings, and this script does not merge those. Review the "
            "list above before applying."
        )

        if not args.apply:
            print("\n(dry run -- pass --apply to write this list to client_config.topic_taxonomy)")
            return 0

        client_config_model = await ClientConfigModel.create_instance(db_client)
        await client_config_model.upsert_client_config(client_id=args.client_id, topic_taxonomy=taxonomy)
        print(f"\nWrote {len(taxonomy)} entries to client_config.topic_taxonomy for client_id={args.client_id!r}.")
        return 0

    finally:
        await engine.dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
