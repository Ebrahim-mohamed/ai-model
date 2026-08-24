from fastapi import APIRouter, Header, HTTPException, Request, status

from .schemes.whatsapp import ChatRequest, ChatResponse

whatsapp_router = APIRouter(
    prefix="/api/whatsapp",
    tags=["whatsapp"],
)


@whatsapp_router.post("/chat", response_model=ChatResponse)
async def chat(body: ChatRequest, request: Request, x_admin_api_key: str = Header(...)):
    """PoC-simulator text endpoint (Implementation Plan Step 1). Resolves
    client_id from the admin API key header — the same mechanism
    routes/sync.py and routes/retrieval.py already use, never a
    client-supplied field. Production's Meta Cloud API webhook adapter is
    a later, additive addition to this same router (a new endpoint plus
    a new stores/whatsapp_transport provider), never a rewrite of this
    handler or of IntentRoutingController."""
    client_id = await request.app.client_config_model.get_client_id_by_admin_api_key(x_admin_api_key)
    if client_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid admin API key")

    result = await request.app.intent_routing_controller.route_turn(
        client_id=client_id,
        session_id=body.session_id,
        modality="text",
        text=body.message,
        brand_filter=body.brand_filter,
    )

    return ChatResponse(
        client_id=client_id,
        session_id=body.session_id,
        reply=result["reply"],
        intent=result["intent"],
        mode=result["mode"],
        debug_json=result["debug_json"],
    )
