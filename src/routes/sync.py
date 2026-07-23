from fastapi import APIRouter, Header, HTTPException, Request, status

from .schemes.sync import SyncTriggerResponse, SyncStatusResponse
from controllers.SyncController import InvalidAdminApiKeyError

sync_router = APIRouter(
    prefix="/api/sync",
    tags=["sync"],
)


@sync_router.post("", response_model=SyncTriggerResponse, status_code=status.HTTP_202_ACCEPTED)
async def trigger_sync(request: Request, x_admin_api_key: str = Header(...)):
    """Human-initiated only — no scheduler, no polling anywhere in this
    codebase triggers this. client_id is resolved server-side from the
    header via SyncController; it is never accepted as a body/path/query
    field, so a tenant can only ever trigger their own sync."""
    try:
        client_id, task_id = await request.app.sync_controller.trigger_sync(admin_api_key=x_admin_api_key)
    except InvalidAdminApiKeyError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid admin API key")
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    return SyncTriggerResponse(task_id=task_id, status="PENDING")


@sync_router.get("/{task_id}/status", response_model=SyncStatusResponse)
async def get_sync_status(task_id: str, request: Request, x_admin_api_key: str = Header(...)):
    try:
        await request.app.sync_controller.resolve_client_id(admin_api_key=x_admin_api_key)
    except InvalidAdminApiKeyError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid admin API key")

    from celery_app import celery_app
    result = celery_app.AsyncResult(task_id)

    return SyncStatusResponse(
        task_id=task_id,
        status=result.status,
        result=str(result.result) if result.ready() else None,
    )
