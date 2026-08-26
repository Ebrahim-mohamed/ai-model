from sqlalchemy import Column, Float, Integer, String
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

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
    #
    # whatsapp_retrieval_top_k_narrow history (2026-08-26): briefly raised
    # 1 -> 3 to fix a real case (18 — a company contracted under one brand
    # system but not the other, needing a second row a single-chunk
    # narrow retrieval couldn't surface). Reverted back to 1 after a
    # real-data retrieval investigation (scripts/investigate_retrieval_
    # task1.py) on the regressions it caused: cases with an overwhelming,
    # unambiguous single correct chunk (top score 7.08, the fact repeated
    # in every retrieved candidate) still failed to extract once wrapped
    # as multiple sources — proving retrieval wasn't the bottleneck, the
    # untrained wrapped/multi-chunk narrow format was. One case also
    # showed a concrete distractor: a different company's row, same field
    # label, different number, only in the window because top_k_narrow
    # exceeded 1. TextReplyController's widening retry (triggers only
    # when the single unwrapped chunk's own extraction comes back
    # completely empty) now carries the "give it more chunks" job
    # instead — same real win for case 18-shaped queries, without paying
    # the untrained-format cost on every narrow turn.
    whatsapp_retrieval_top_k_narrow = Column(Integer, nullable=False, server_default="1")
    whatsapp_retrieval_top_k_broad = Column(Integer, nullable=False, server_default="5")

    # Deterministic breadth classification — replaces the LLM-based
    # classify_intent(["narrow","broad"]) call, which real traffic proved
    # unreliable: clearly-narrow questions (e.g. "عندي تأمين بس عايز
    # ادفع كاش، هاخد لاكي برضه؟") were misrouted to the broad path, and
    # every literal [BEGIN SOURCE] scaffolding leak found in production
    # output traces back to exactly that misrouting (the broad path is
    # the only one that wraps chunks that way) — meaning
    # FieldSelectionController never even ran on those turns.
    #
    # Real calibration against the live reranker (cross-encoder, raw
    # unbounded logit score) on 5 genuinely narrow + 3 genuinely broad
    # queries: narrow top-1 scores were 5.19, -0.02, 5.99, 0.39, 7.08;
    # broad top-1 scores were -1.24, -2.69, -0.10. A flat top-score
    # threshold of 0.0 correctly separates 4/5 narrow and 3/3 broad in
    # that sample — a real, evidence-based number, not the illustrative
    # ">1.0" first floated for this design, which would have misclassified
    # 2 of the 5 real narrow samples (the ones with weaker, more
    # paraphrased wording) as broad.
    #
    # whatsapp_breadth_score_gap (the required drop-off to results[1]) is
    # a weaker signal in the same real data — broad queries sometimes show
    # a LARGER gap than narrow ones (e.g. "عندكم أشعة إيه؟", broad,
    # gap=1.07 vs. "موافقة تأمين بنك مصر...", narrow, gap=0.71) — so this
    # is kept as a lenient secondary condition (AND'd with the top-score
    # check, per the original design), not the primary discriminator.
    #
    # Known, disclosed limitation: a genuinely narrow question phrased as
    # an indirect paraphrase rather than a direct term match can still
    # score low on the reranker (real example: "هو فرع سليمان أباظة
    # مناسب لو معايا حد بكرسي متحرك؟" scored -0.02, just under this
    # threshold) and get treated as broad. That's a real, different
    # failure mode from the one this fix targets (LLM-based
    # misclassification) — deterministic and tunable instead of
    # unpredictable, but not perfect.
    whatsapp_breadth_score_threshold = Column(Float, nullable=False, server_default="0.0")
    whatsapp_breadth_score_gap = Column(Float, nullable=False, server_default="0.3")

    # Config-driven, VALUE-matched brand promotion to top-level metadata
    # (claude.md §1.3 — never a hardcoded column/sheet name in a
    # controller). Superseded label-keyed metadata_promotion_map after
    # real data showed brand info appears under 4 different column
    # spellings (account/Account/Accounts/Acoount) across 5 sheets, in 2
    # different scripts (Examinations: English "Technoscan"/"Cairoscan";
    # Branch Directory/Sheet1/Weights/Anesthesia: Arabic
    # "تكنوسكان"/"كايروسكان") — a label-keyed map breaks on the very next
    # sheet that spells the column yet another way. This maps real brand
    # VALUES (in either language) to one canonical lowercase value.
    # ChunkingController scans every field's value, not its column name,
    # against this map — so a brand-new sheet with the same column under
    # any other spelling is picked up automatically, zero code/config
    # change. Only the business vocabulary itself (a genuinely new brand,
    # or a new way of writing an existing one) needs a one-time edit here
    # — the same boundary claude.md §3.1 already draws everywhere else
    # (never infer business semantics automatically).
    brand_value_aliases = Column(JSONB, nullable=False, server_default="{}")
