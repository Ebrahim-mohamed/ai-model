from abc import ABC, abstractmethod


class ClientNotOnboardedError(Exception):
    """Raised when a client_id has no persisted token cache, or its refresh
    token has been revoked/expired. Never a silent fallback to another
    tenant's cache — the one-time Device Code Flow must be re-run for this
    client only, restoring service without touching any other tenant."""


class OneDriveInterface(ABC):
    """Port every OneDrive-alternative adapter (MSALGraphProvider today, a
    future SharePoint provider tomorrow) must implement. client_id is a
    required, no-default argument on every method — see claude.md §1.3."""

    @abstractmethod
    async def authenticate(self, client_id: str) -> str:
        pass

    @abstractmethod
    async def fetch_file(self, client_id: str, item_id: str) -> bytes:
        pass
