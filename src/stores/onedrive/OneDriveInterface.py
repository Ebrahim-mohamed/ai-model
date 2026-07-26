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

    @abstractmethod
    def fetch_files_in_folder(self, client_id: str, drive_id: str, folder_id: str):
        """Async generator yielding (file_name, file_bytes) for every
        .xlsx item directly inside the given shared folder — never a
        sub-folder's contents recursively, and never another client's
        drive/folder. client_id is required, no default, exactly like
        every other method here (claude.md §1.3). drive_id/folder_id are
        per-client values the caller reads from client_config — this
        method never hardcodes or defaults either."""
        pass
