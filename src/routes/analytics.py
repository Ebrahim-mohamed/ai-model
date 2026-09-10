from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Header, HTTPException, Query, Request, status

from .schemes.analytics import DashboardResponse

analytics_router = APIRouter(
    prefix="/api/analytics",
    tags=["analytics"],
)


@analytics_router.get("/dashboard", response_model=DashboardResponse)
async def dashboard(
    request: Request,
    x_admin_api_key: str = Header(...),
    date_from: datetime | None = Query(None),
    date_to: datetime | None = Query(None),
):
    """client_id is resolved server-side from the admin API key header —
    the same mechanism every other route in this codebase uses (routes/
    sync.py, routes/retrieval.py, routes/whatsapp.py), never a
    client-supplied field, so a tenant can only ever see their own
    analytics. date_from/date_to default to the trailing 30 days when
    omitted — an explicit, disclosed default rather than an unbounded
    query with no range at all."""
    client_id = await request.app.client_config_model.get_client_id_by_admin_api_key(x_admin_api_key)
    if client_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid admin API key")

    resolved_to = date_to or datetime.now(timezone.utc)
    resolved_from = date_from or (resolved_to - timedelta(days=30))

    result = await request.app.analytics_controller.get_dashboard(
        client_id=client_id, date_from=resolved_from, date_to=resolved_to,
    )
    return DashboardResponse(**result)
