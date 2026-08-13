from enum import Enum


class GenerationEnums(Enum):
    """Closed set of conversational-model adapters — mapped to a concrete
    class only inside GenerationProviderFactory (claude.md §1.2). Qwen2.5-
    7B-Instruct is the Implementation Plan's chosen model (Section 3 Step
    1); a future fine-tuned checkpoint or a different vendor is a new
    providers/ file plus one new value here, never a rewrite of this set."""

    QWEN2_5_7B_INSTRUCT = "QWEN2_5_7B_INSTRUCT"
