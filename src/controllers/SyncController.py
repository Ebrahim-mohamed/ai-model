from .BaseController import BaseController


class InvalidAdminApiKeyError(Exception):
    """Raised when the provided admin API key doesn't resolve to any
    client_config row. routes/sync.py catches this and maps it to 401 —
    routes never touch the DB themselves to make that determination."""


class SyncController(BaseController):
    """Resolves tenant context and enqueues the fetch task. Never touches
    Request/Response objects, never writes raw SQL — only calls the
    injected repository and the Celery task's .delay()."""

    def __init__(self, client_config_model):
        super().__init__()
        self.client_config_model = client_config_model

    async def resolve_client_id(self, admin_api_key: str) -> str:
        client_id = await self.client_config_model.get_client_id_by_admin_api_key(admin_api_key)
        if client_id is None:
            raise InvalidAdminApiKeyError("No client_config row matches this admin API key")
        return client_id

    async def trigger_sync(self, admin_api_key: str) -> tuple[str, str]:
        """Resolves client_id, confirms an onedrive_item_id is configured,
        and enqueues the fetch task — never blocks waiting for the fetch/
        parse/chunk pipeline to finish. Returns (client_id, task_id)."""
        client_id = await self.resolve_client_id(admin_api_key)

        client_config = await self.client_config_model.get_client_config(client_id)
        if client_config is None or not client_config.onedrive_item_id:
            raise ValueError(f"client_id={client_id!r} has no onedrive_item_id configured in client_config")

        from tasks.onedrive_sync import fetch_and_dispatch
        task = fetch_and_dispatch.delay(client_id=client_id)
        return client_id, task.id
