from enum import Enum


class RerankerEnums(Enum):
    """Closed set of re-ranking provider adapters — mapped to a concrete
    class only inside RerankerProviderFactory.

    2026-09-02: BGE_RERANKER_V2_M3 added as a togglable A/B candidate
    against the deployed CROSS_ENCODER default (RERANKER_BACKEND config,
    switch via .env — never a code change to pick one). CROSS_ENCODER
    stays the default; see CrossEncoderProvider/BGERerankerV2M3Provider's
    own docstrings for the real evidence behind evaluating the swap."""

    CROSS_ENCODER = "CROSS_ENCODER"
    BGE_RERANKER_V2_M3 = "BGE_RERANKER_V2_M3"
