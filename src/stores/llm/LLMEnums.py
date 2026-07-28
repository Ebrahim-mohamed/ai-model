from enum import Enum


class LLMEnums(Enum):
    """Closed set of embedding-model adapters — mapped to a concrete
    class only inside LLMProviderFactory. Both values exist here even
    though SWAN_LARGE cannot actually run on this dev machine yet (see
    README's Step 8 section) — the interface contract is complete, and
    swapping in real Swan-Large access later is a config flip, not a
    rewrite."""

    BGE_M3 = "BGE_M3"
    SWAN_LARGE = "SWAN_LARGE"
