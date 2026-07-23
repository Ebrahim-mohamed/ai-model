from enum import Enum


class OneDriveEnums(Enum):
    """Closed set of OneDrive provider adapters — mapped to a concrete
    class only inside OneDriveProviderFactory."""

    MSAL_GRAPH = "MSAL_GRAPH"


class AuthFlowEnums(Enum):
    """Closed set of MSAL auth flows. DEVICE_CODE is the only one valid for
    OneDrive Personal (claude.md §2.3) — no Client Credentials Flow, ever."""

    DEVICE_CODE = "DEVICE_CODE"


class TokenCacheBackendEnums(Enum):
    """Closed set of where the encrypted token-cache blob is persisted."""

    POSTGRES = "POSTGRES"
