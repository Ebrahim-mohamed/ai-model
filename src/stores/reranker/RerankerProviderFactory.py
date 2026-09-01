from .RerankerEnums import RerankerEnums
from .providers import CrossEncoderProvider, BGERerankerV2M3Provider


class RerankerProviderFactory:
    """The only code allowed to map a RERANKER_BACKEND config string to a
    concrete adapter — identical pattern to LLMProviderFactory/
    VectorDBProviderFactory/OneDriveProviderFactory. A new reranking
    vendor is one new providers/ file plus one branch here."""

    def __init__(self, config):
        self.config = config

    def create(self, provider: str):
        if provider == RerankerEnums.CROSS_ENCODER.value:
            return CrossEncoderProvider()

        if provider == RerankerEnums.BGE_RERANKER_V2_M3.value:
            return BGERerankerV2M3Provider()

        return None
