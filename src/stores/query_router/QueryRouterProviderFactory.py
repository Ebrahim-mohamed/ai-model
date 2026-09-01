from .QueryRouterEnums import QueryRouterEnums
from .providers import NileChat12BBaseProvider


class QueryRouterProviderFactory:
    """The only code allowed to map a QUERY_ROUTER_BACKEND config string
    to a concrete adapter — identical pattern to GenerationProviderFactory
    / VectorDBProviderFactory / OneDriveProviderFactory (claude.md §1.2).
    A future lightweight candidate is one new branch here plus one new
    providers/ file, never a controller change."""

    def __init__(self, config):
        self.config = config

    def create(self, provider: str):
        if provider == QueryRouterEnums.NILE_CHAT_12B_BASE.value:
            return NileChat12BBaseProvider(
                base_url=self.config.QUERY_ROUTER_BASE_URL,
                model_name=self.config.QUERY_ROUTER_MODEL_NAME,
                request_timeout_seconds=self.config.QUERY_ROUTER_REQUEST_TIMEOUT_SECONDS,
            )

        return None
