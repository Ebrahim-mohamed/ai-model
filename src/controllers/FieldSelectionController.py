import logging

import numpy as np

from .BaseController import BaseController


class FieldSelectionController(BaseController):
    """Section 3 Step 1 generation-quality fix: narrows a retrieved
    chunk's structured field_data (see ChunkingController) down to just
    the field(s) that actually answer the patient's question, before it
    ever reaches the LLM — replacing "hand the model 15 fields and trust
    it to only mention the 1 that was asked about" with "hand the model
    only the 1-2 fields that matched." A different grain of concern than
    RetrievalController (which chunk) or TextReplyController (how to
    reply) — a genuinely new capability, so it gets its own file rather
    than being folded into either (claude.md §1.2).

    Fully dynamic, per the standing no-hardcoding constraint: this
    controller never references a specific field name, sheet, or
    keyword. It ranks whatever keys `field_data` actually contains
    against the query by embedding similarity — the same mechanism
    (BGE-M3 via the shared embedding_client) already used for chunk
    retrieval itself, just applied one grain finer. A brand-new sheet
    with fields nobody has seen before works identically — there is
    nothing here to update when the schema changes.

    client_id is deliberately not a parameter here (same precedent
    RerankerInterface.rerank already documents): this is pure
    computation over a dict the caller already fetched under a
    tenant-scoped retrieval call, never storage/DB access of its own.
    """

    def __init__(self, embedding_client):
        super().__init__()
        self.embedding_client = embedding_client
        self.logger = logging.getLogger(__name__)

    async def select_relevant_fields(
        self,
        query: str,
        field_data: dict,
        min_similarity: float,
        max_fields: int,
    ) -> dict:
        """Returns the subset of field_data whose label matches `query`
        closely enough — ranked by cosine similarity between the query
        embedding and each field label's embedding, capped at
        max_fields, filtered to scores at or above min_similarity.

        Returns an empty dict (never raises) if field_data is empty or
        nothing clears the similarity floor — callers fall back to the
        full chunk content in that case, the same fallback path used for
        chunks synced before this field existed at all."""
        if not field_data:
            return {}

        labels = list(field_data.keys())

        query_vectors = await self.embedding_client.embed_text([query], is_query=True)
        label_vectors = await self.embedding_client.embed_text(labels, is_query=False)

        query_vector = np.array(query_vectors[0])
        label_matrix = np.array(label_vectors)

        # Cosine similarity via plain vector math — not a keyword match,
        # not a regex, not a second LLM call. Guards against a zero-norm
        # vector (a degenerate/whitespace-only label) dividing by zero
        # rather than assuming every embedding is well-formed.
        query_norm = np.linalg.norm(query_vector)
        label_norms = np.linalg.norm(label_matrix, axis=1)
        denom = label_norms * query_norm
        similarities = np.divide(
            label_matrix @ query_vector,
            denom,
            out=np.zeros_like(denom),
            where=denom != 0,
        )

        ranked = sorted(zip(labels, similarities), key=lambda pair: pair[1], reverse=True)

        selected = {
            label: field_data[label]
            for label, score in ranked[:max_fields]
            if score >= min_similarity
        }

        # DEBUG: every candidate key that was evaluated, and its exact
        # similarity score against the query — the full ranked list, not
        # just the winners, so a specific "why wasn't field X picked" can
        # actually be answered from the log.
        self.logger.debug(
            f"[field_selection] query={query!r} all_candidates="
            f"{[(label, round(float(score), 4)) for label, score in ranked]}"
        )

        if selected:
            self.logger.info(
                f"[field_selection] query={query!r} selected={list(selected.keys())} "
                f"out of {len(labels)} candidate fields (min_similarity={min_similarity}, max_fields={max_fields})"
            )
        else:
            self.logger.info(
                f"[field_selection] query={query!r} matched none of {len(labels)} candidate "
                f"fields above min_similarity={min_similarity} — caller should fall back to full chunk content"
            )

        return selected
