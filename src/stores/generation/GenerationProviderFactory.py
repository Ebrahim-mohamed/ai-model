from .GenerationEnums import GenerationEnums
from .providers import QwenProvider


class GenerationProviderFactory:
    """The only code allowed to map a GENERATION_BACKEND config string to
    a concrete adapter — identical pattern to LLMProviderFactory /
    VectorDBProviderFactory / OneDriveProviderFactory (claude.md §1.2)."""

    def __init__(self, config):
        self.config = config

    def create(self, provider: str):
        if provider == GenerationEnums.QWEN2_5_7B_INSTRUCT.value:
            return QwenProvider(
                base_url=self.config.GENERATION_BASE_URL,
                model_name=self.config.GENERATION_MODEL_NAME,
                request_timeout_seconds=self.config.GENERATION_REQUEST_TIMEOUT_SECONDS,
            )

        return None
