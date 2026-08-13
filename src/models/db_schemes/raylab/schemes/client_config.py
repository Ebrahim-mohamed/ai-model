from sqlalchemy import Column, Float, Integer, String
from sqlalchemy.dialects.postgresql import ARRAY

from .raylab_base import SQLAlchemyBase


class ClientConfig(SQLAlchemyBase):
    """The tenant registry. One row per client_id — every value that would
    otherwise be a hardcoded Python constant lives here instead."""

    __tablename__ = "client_config"

    client_id = Column(String, primary_key=True)
    allowed_metadata_keys = Column(ARRAY(String), nullable=False, server_default="{}")

    # The client's shared OneDrive sync folder: onedrive_item_id is that
    # folder's own Item ID (not a single workbook's — every .xlsx file
    # directly inside it is fetched and parsed on each sync), and
    # onedrive_drive_id is the driveId it lives on. Both are per-client,
    # DB-sourced values — never hardcoded in stores/onedrive.
    onedrive_item_id = Column(String, nullable=True)
    onedrive_drive_id = Column(String, nullable=True)

    embedding_backend_override = Column(String, nullable=True)

    # Step 4 — the minimal "authenticated admin session" stand-in: POST
    # /api/sync resolves client_id from this key server-side, never from a
    # client-supplied field. Section 2 has no full admin-auth system in
    # scope; this is the smallest mechanism that still makes the resolution
    # server-controlled rather than trusting the caller's word for it.
    admin_api_key = Column(String, nullable=True, unique=True)

    # Step 9 — hybrid retrieval business thresholds. Never a hardcoded
    # Python constant (claude.md §1.3 names top-K and RRF's k explicitly):
    # a client with a small, dense knowledge base and one with a huge,
    # noisy one plausibly want different values here.
    retrieval_top_k = Column(Integer, nullable=False, server_default="5")
    rrf_k = Column(Integer, nullable=False, server_default="60")

    # Section 3 Step 1 — Mode A's query-breadth-aware retrieval ceilings.
    # Two separate fields, not one: a narrow factual question and a broad
    # "what do you offer" question need genuinely different chunk counts,
    # and both must stay config-driven, never a literal inside
    # TextReplyController (claude.md §1.3, §6.3, Implementation Plan Step 1).
    whatsapp_retrieval_top_k_narrow = Column(Integer, nullable=False, server_default="1")
    whatsapp_retrieval_top_k_broad = Column(Integer, nullable=False, server_default="5")

    # Relevance gate for narrow-breadth Mode A queries only (claude.md
    # §1.3 — a similarity cutoff is exactly the kind of business
    # threshold that must live here, never a Python constant). Deliberately
    # NOT applied to broad queries: real calibration against the live
    # reranker (cross-encoder/mmarco-mMiniLMv2-L12-H384-v1, raw logit
    # score, unbounded) showed a genuinely in-domain broad query
    # ("عندكم أشعة إيه؟", top score -1.24) scores in the same range as a
    # genuinely out-of-domain one ("بتعملوا عمليات قلب مفتوح؟", -1.30) —
    # broad questions don't match any single chunk well even when
    # correct, so a flat cutoff there would false-decline real queries.
    # Narrow queries showed a clean, wide gap instead: real in-domain
    # top-1 scores of 5.13 and 0.86 vs. -1.3 to -5.8 for every
    # out-of-domain/adjacent/nonsense query tested — 0.0 sits well clear
    # of every real sample on both sides.
    whatsapp_min_relevance_score = Column(Float, nullable=False, server_default="0.0")
