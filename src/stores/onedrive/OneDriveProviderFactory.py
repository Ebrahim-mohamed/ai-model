from .OneDriveEnums import OneDriveEnums
from .providers import MSALGraphProvider


class OneDriveProviderFactory:
    """The only code allowed to map an ONEDRIVE_AUTH_BACKEND config string
    to a concrete adapter — identical pattern to LLMProviderFactory /
    VectorDBProviderFactory. A new OneDrive-alternative (e.g. SharePoint)
    is one new providers/ file plus one branch here."""

    def __init__(self, config, token_cache_model):
        self.config = config
        self.token_cache_model = token_cache_model

    def create(self, provider: str):
        if provider == OneDriveEnums.MSAL_GRAPH.value:
            return MSALGraphProvider(
                token_cache_model=self.token_cache_model,
                msal_client_id=self.config.MSAL_CLIENT_ID,
            )

        return None
