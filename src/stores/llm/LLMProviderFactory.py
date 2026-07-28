from .LLMEnums import LLMEnums
from .providers.BGEM3Provider import BGEM3Provider
from .providers.SwanLargeProvider import SwanLargeProvider


class LLMProviderFactory:
    """The only code allowed to map an EMBEDDING_BACKEND config string to
    a concrete adapter — identical pattern to OneDriveProviderFactory /
    VectorDBProviderFactory. A new embedding candidate is one new
    providers/ file plus one branch here; nothing in ChunkingController,
    RetrievalController, or EmbeddingShootoutController changes."""

    def __init__(self, config):
        self.config = config

    def create(self, provider: str):
        if provider == LLMEnums.BGE_M3.value:
            return BGEM3Provider()
        if provider == LLMEnums.SWAN_LARGE.value:
            return SwanLargeProvider(hf_token=self.config.HF_TOKEN)

        return None
