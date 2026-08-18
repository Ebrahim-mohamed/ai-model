from enum import Enum


class GenerationEnums(Enum):
    """Closed set of conversational-model adapters — mapped to a concrete
    class only inside GenerationProviderFactory (claude.md §1.2). Qwen2.5-
    7B-Instruct is the Implementation Plan's chosen model (Section 3 Step
    1); a future fine-tuned checkpoint or a different vendor is a new
    providers/ file plus one new value here, never a rewrite of this set.

    Phase 0 model-upgrade bake-off (see README's Phase 0 section): each
    candidate gets its own value here and its own provider file, exactly
    like EMBEDDING_BACKEND's BGE_M3/SWAN_LARGE shootout in Step 8 — never
    reusing QWEN2_5_7B_INSTRUCT's label for a different model actually
    running behind GENERATION_BASE_URL, which would corrupt which real
    model produced a given golden-suite output."""

    QWEN2_5_7B_INSTRUCT = "QWEN2_5_7B_INSTRUCT"
    FALCON_H1_34B_INSTRUCT = "FALCON_H1_34B_INSTRUCT"
    NILE_CHAT_12B = "NILE_CHAT_12B"
    QWEN2_5_32B_INSTRUCT_AWQ = "QWEN2_5_32B_INSTRUCT_AWQ"
