import asyncio
import logging

import msal
import requests

from ..OneDriveInterface import OneDriveInterface, ClientNotOnboardedError

# Personal Microsoft accounts only (claude.md §2.3 — OneDrive Personal, not
# a Business tenant) — the /consumers authority is what makes Device Code
# Flow + a public client app valid here; /common or /organizations are wrong
# for this account type.
DEFAULT_AUTHORITY = "https://login.microsoftonline.com/consumers"
GRAPH_SCOPES = ["Files.Read"]
GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"


class MSALGraphProvider(OneDriveInterface):
    """MSAL Device Code Flow against OneDrive Personal, via Microsoft Graph.

    Delegates all token-cache persistence to the injected TokenCacheModel —
    this class never opens its own DB session and never writes raw SQL; it
    only calls the repository it was constructed with, exactly like every
    other store/provider in this codebase depends on its injected
    collaborators rather than reaching around them.

    msal and requests are both synchronous libraries; their calls are
    offloaded via asyncio.to_thread so a slow Graph API round-trip or a
    blocking token refresh never stalls the event loop for other tenants.
    """

    def __init__(self, token_cache_model, msal_client_id: str, authority: str = DEFAULT_AUTHORITY):
        self.token_cache_model = token_cache_model
        self.msal_client_id = msal_client_id
        self.authority = authority
        self.logger = logging.getLogger(__name__)

    def _build_app(self, cache: msal.SerializableTokenCache) -> msal.PublicClientApplication:
        return msal.PublicClientApplication(
            self.msal_client_id,
            authority=self.authority,
            token_cache=cache,
        )

    async def authenticate(self, client_id: str) -> str:
        """Silently exchanges this client's persisted refresh token for a
        fresh access token — no browser, no human interaction. Raises
        ClientNotOnboardedError if no cache was ever persisted for this
        client_id, or if silent acquisition fails (refresh token revoked/
        expired) — in both cases the fix is re-running the Device Code Flow
        for this client only; no other tenant's sync is ever affected."""
        serialized_cache = await self.token_cache_model.get_token_cache(client_id)
        if serialized_cache is None:
            raise ClientNotOnboardedError(
                f"No token cache for client_id={client_id!r}. Run "
                "register_client_via_device_flow(client_id) once before syncing."
            )

        cache = msal.SerializableTokenCache()
        cache.deserialize(serialized_cache)
        app = self._build_app(cache)

        accounts = await asyncio.to_thread(app.get_accounts)
        if not accounts:
            raise ClientNotOnboardedError(
                f"Token cache for client_id={client_id!r} has no account on record. "
                "Re-run the Device Code Flow for this client."
            )

        result = await asyncio.to_thread(app.acquire_token_silent, GRAPH_SCOPES, account=accounts[0])
        if not result or "access_token" not in result:
            raise ClientNotOnboardedError(
                f"Silent token acquisition failed for client_id={client_id!r} "
                "(refresh token likely revoked or expired). Re-run the Device Code "
                "Flow for this client only — other tenants are unaffected."
            )

        if cache.has_state_changed:
            await self.token_cache_model.save_token_cache(client_id, cache.serialize())

        return result["access_token"]

    async def fetch_file(self, client_id: str, item_id: str) -> bytes:
        access_token = await self.authenticate(client_id)

        response = await asyncio.to_thread(
            requests.get,
            f"{GRAPH_BASE_URL}/me/drive/items/{item_id}/content",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        response.raise_for_status()
        return response.content

    async def fetch_files_in_folder(self, client_id: str, drive_id: str, folder_id: str):
        """Lists the given shared folder's children on the given drive and
        yields (file_name, file_bytes) for every direct child that is
        actually a file (not a sub-folder) whose name ends in .xlsx.
        drive_id/folder_id are per-client, DB-sourced (client_config) —
        never hardcoded and never accepted from anything but that client's
        own configuration, so this can never be pointed at another
        tenant's drive."""
        access_token = await self.authenticate(client_id)

        url = f"{GRAPH_BASE_URL}/drives/{drive_id}/items/{folder_id}/children"
        while url:
            response = await asyncio.to_thread(
                requests.get,
                url,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            response.raise_for_status()
            payload = response.json()

            for item in payload.get("value", []):
                name = item.get("name", "")
                # "file" in item excludes sub-folders (a DriveItem carries a
                # "file" or "folder" facet, never both) — belt-and-suspenders
                # alongside the name check, since attempting /content on a
                # folder would otherwise just fail with a confusing error.
                if "file" not in item or not name.endswith(".xlsx"):
                    continue

                file_response = await asyncio.to_thread(
                    requests.get,
                    f"{GRAPH_BASE_URL}/drives/{drive_id}/items/{item['id']}/content",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                file_response.raise_for_status()
                yield name, file_response.content

            # Graph paginates folder listings — follow @odata.nextLink
            # rather than silently truncating a folder with many files.
            url = payload.get("@odata.nextLink")

    async def register_client_via_device_flow(self, client_id: str) -> None:
        """The one-time, human-supervised onboarding step (Section 2's
        Device Code Flow setup) — never called by authenticate()/
        fetch_file(). Run this once per client; it blocks (interactively)
        until the operator completes sign-in at microsoft.com/devicelogin,
        then persists the resulting cache via TokenCacheModel."""
        cache = msal.SerializableTokenCache()
        app = self._build_app(cache)

        flow = await asyncio.to_thread(app.initiate_device_flow, scopes=GRAPH_SCOPES)
        if "user_code" not in flow:
            raise RuntimeError(f"Failed to start device flow for client_id={client_id!r}: {flow}")

        print(flow["message"])  # "To sign in, visit https://microsoft.com/devicelogin and enter code ..."
        result = await asyncio.to_thread(app.acquire_token_by_device_flow, flow)

        if "access_token" not in result:
            raise RuntimeError(
                f"Device flow failed for client_id={client_id!r}: {result.get('error_description')}"
            )

        # The device-flow wait above is human-paced — anywhere from seconds
        # to minutes — and this is the first time this call touches the DB
        # at all, so it's also the first connection attempt on this engine.
        # A long enough wait can outlast a Docker/WSL2 network hiccup that
        # has nothing to do with Postgres itself; retry a few times rather
        # than losing a completed, hard-won interactive login to a
        # transient connection error.
        last_error = None
        for attempt in range(1, 4):
            try:
                await self.token_cache_model.save_token_cache(client_id, cache.serialize())
                break
            except Exception as e:
                last_error = e
                self.logger.warning(
                    f"save_token_cache attempt {attempt}/3 failed for client_id={client_id!r}: {e!r}"
                )
                if attempt < 3:
                    await asyncio.sleep(2 * attempt)
        else:
            raise RuntimeError(
                f"Device flow succeeded for client_id={client_id!r} but persisting the token "
                f"cache failed after 3 attempts: {last_error!r}. The access token was NOT saved — "
                "re-run register_client_via_device_flow for this client (the device flow itself "
                "will need to be redone too, since the token wasn't persisted)."
            ) from last_error

        self.logger.info(f"Token cache persisted for client_id={client_id!r}")
