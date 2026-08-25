from .BaseController import BaseController
from utils.intent_routing_map import get_routing_target, get_allowed_intents, RoutingTarget
from models.IntentLogModel import RoutingOutcome
from models.ChatHistoryModel import MessageDirection
from stores.llm.templates.template_parser import TemplateParser, TemplateBucket

PENDING_PHASE_REPLY = (
    "الخدمة دي لسه تحت التجهيز حاليًا، هوصلك بأقرب موظف عشان يقدر يساعدك في طلبك ده."
)


class IntentRoutingController(BaseController):
    """The single, shared intent-classification-and-routing gate every
    inbound turn passes through (Implementation Plan §Foundation,
    claude.md §6.3). Classifies once, dispatches by data-driven lookup
    (utils/intent_routing_map.py) — never a per-intent `if` chain — and
    is the one call site that writes to the Intent Log and both tiers of
    chat history, so no phase re-implements logging or persistence on
    its own."""

    def __init__(
        self,
        generation_client,
        text_reply_controller,
        chat_history_model,
        session_store,
    ):
        super().__init__()
        self.generation_client = generation_client
        self.text_reply_controller = text_reply_controller
        self.chat_history_model = chat_history_model
        self.session_store = session_store
        self.template_parser = TemplateParser()

    async def route_turn(
        self, client_id: str, session_id, modality: str, text: str, brand_filter: str | None = None,
    ) -> dict:
        session_state = await self.session_store.get_or_hydrate_session(
            client_id, session_id, self.chat_history_model,
        )

        # Applied once, right here, immediately after hydration — the same
        # place session_state is already read and (at the end of this
        # method) persisted, so the write path isn't scattered across
        # callers. None (the route's own default when the field is
        # omitted from the request) means "no change this turn" — the
        # session keeps whatever brand_filter it already had. "all" is an
        # explicit clear, distinct from silence.
        if brand_filter is not None:
            session_state["brand_filter"] = None if brand_filter == "all" else brand_filter

        # Real worked examples (a broad "what do you offer" question, an
        # out-of-scope medical question) — the bare closed-set label list
        # alone was observed misclassifying both on real test traffic.
        intent_guidance = self.template_parser.resolve(TemplateBucket.C, "whatsapp_intent_classification_guidance")
        intent = await self.generation_client.classify_intent(
            text, get_allowed_intents(), history=session_state.get("history", []), guidance=intent_guidance,
        )
        target = get_routing_target(intent)

        mode = None
        debug_json = None
        if target == RoutingTarget.TEXT_PIPELINE.value:
            reply_text, mode, debug_json = await self.text_reply_controller.reply(
                client_id, session_id, text, session_state,
            )
            routing_outcome = RoutingOutcome.AI_HANDLED
        else:
            # Steps 2/5 (complaints, booking) aren't built yet — routed
            # here honestly rather than silently mishandled by Step 1's
            # text pipeline, or crashing.
            reply_text = PENDING_PHASE_REPLY
            routing_outcome = RoutingOutcome.NOT_IMPLEMENTED

        await self.chat_history_model.append_message(client_id, session_id, MessageDirection.INBOUND, text)
        await self.chat_history_model.append_message(client_id, session_id, MessageDirection.OUTBOUND, reply_text)

        window = self.session_store.history_window
        session_state["history"] = (session_state.get("history", []) + [
            {"role": "user", "content": text},
            {"role": "assistant", "content": reply_text},
        ])[-window:]
        await self.session_store.save_session(client_id, session_id, session_state)

        # Fire-and-forget analytics write — enqueued, never awaited
        # inline, so a logging failure or delay can never affect the
        # patient-facing reply (claude.md §6, Implementation Plan Step 1).
        from tasks.log_intent import log_intent_turn
        log_intent_turn.delay(
            client_id=client_id,
            session_id=str(session_id),
            modality=modality,
            intent=intent,
            routing_outcome=routing_outcome,
        )

        return {"reply": reply_text, "intent": intent, "mode": mode, "debug_json": debug_json}
