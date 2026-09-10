from fastapi import APIRouter, Header, HTTPException, Request, status

from helpers.bidi_text import sanitize_reply_text
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

    # 2026-09-09 addendum — real, confirmed patient-facing WhatsApp bugs
    # across BOTH standard direct answers and the clarification menu:
    # dense unspaced text, glued Arabic/English punctuation, inverted
    # leading colons, and malformed/orphaned parentheses (readability),
    # plus jumbled mixed-direction rendering (a Unicode Bidirectional
    # Algorithm consequence of embedding Latin/digit runs in RTL prose).
    # sanitize_reply_text (see helpers/bidi_text.py's own docstring) is
    # the single, uniform pipeline for both output shapes: punctuation/
    # spacing normalization, then paragraph spacing (a no-op for the
    # clarification menu's own already-multi-line shape), then bidi
    # isolation last.
    #
    # Applied ONLY here, on the outward-facing copy — never inside
    # IntentRoutingController/TextReplyController's own generation logic
    # (the clarification path's own per-suffix normalize_bilingual_
    # punctuation call at construction time is the one exception, and it
    # produces ordinary visible characters, never an invisible control
    # character, so it's safe to also bake into what gets persisted), and
    # never on what gets written to chat_history/session history for the
    # bidi-isolation step specifically (route_turn already persisted the
    # real, unmodified `result["reply"]` before this line runs) — so no
    # invisible control character ever reaches a future LLM prompt via
    # conversation history, only this one HTTP response.
    sanitized_reply = sanitize_reply_text(result["reply"])

    return ChatResponse(
        client_id=client_id,
        session_id=body.session_id,
        reply=sanitized_reply,
        intent=result["intent"],
        mode=result["mode"],
        debug_json=result["debug_json"],
    )
