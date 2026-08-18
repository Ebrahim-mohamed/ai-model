from .GenerationEnums import GenerationEnums
from .providers import QwenProvider, FalconH1Provider, NileChatProvider


class GenerationProviderFactory:
    """The only code allowed to map a GENERATION_BACKEND config string to
    a concrete adapter — identical pattern to LLMProviderFactory /
    VectorDBProviderFactory / OneDriveProviderFactory (claude.md §1.2).

    Phase 0 bake-off: each new candidate is one new branch here plus one
    new providers/ file — a Qwen2.5-32B-AWQ branch follows the identical
    pattern once its own model-specific constants (stop sequences,
    sampling defaults) are confirmed from its real chat template, never
    guessed."""

    def __init__(self, config):
        self.config = config

    def create(self, provider: str):
        if provider == GenerationEnums.QWEN2_5_7B_INSTRUCT.value:
            return QwenProvider(
                base_url=self.config.GENERATION_BASE_URL,
                model_name=self.config.GENERATION_MODEL_NAME,
                request_timeout_seconds=self.config.GENERATION_REQUEST_TIMEOUT_SECONDS,
            )
        if provider == GenerationEnums.FALCON_H1_34B_INSTRUCT.value:
            return FalconH1Provider(
                base_url=self.config.GENERATION_BASE_URL,
                model_name=self.config.GENERATION_MODEL_NAME,
                request_timeout_seconds=self.config.GENERATION_REQUEST_TIMEOUT_SECONDS,
            )
        if provider == GenerationEnums.NILE_CHAT_12B.value:
            return NileChatProvider(
                base_url=self.config.GENERATION_BASE_URL,
                model_name=self.config.GENERATION_MODEL_NAME,
                request_timeout_seconds=self.config.GENERATION_REQUEST_TIMEOUT_SECONDS,
            )

        return None
