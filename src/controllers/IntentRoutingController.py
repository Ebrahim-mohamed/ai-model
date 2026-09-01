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
        query_router_client,
        text_reply_controller,
        chat_history_model,
        session_store,
    ):
        super().__init__()
        self.query_router_client = query_router_client
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

        # Contextual Query Reformulation (CQR), 2026-09-01 — runs FIRST,
        # unconditionally, on every turn, before intent is even known.
        # Real production evidence (three separate prompt-engineering
        # rounds, all still showing the same anaphora-resolution failure
        # whenever rewriting was bundled with or made conditional on
        # classification) drove this reordering: a dedicated,
        # single-purpose rewrite step run up front removes the need for
        # classify_intent to reason about pronouns/history at all — it
        # now always receives an already-standalone query. Trade-off,
        # accepted deliberately rather than silently: this replaces the
        # earlier "only rewrite when the intent needs it" efficiency gain
        # with a flat two-calls-every-turn cost, since routing can no
        # longer be decided before rewriting happens.
        rewrite_guidance = self.template_parser.resolve(TemplateBucket.C, "whatsapp_query_rewrite_guidance")
        standalone_query = await self.query_router_client.rewrite_query(
            text, session_state.get("history", []), guidance=rewrite_guidance,
        )

        # Real worked examples (a broad "what do you offer" question, an
        # out-of-scope medical question) — the bare closed-set label list
        # alone was observed misclassifying real test traffic.
        intent_guidance = self.template_parser.resolve(TemplateBucket.C, "whatsapp_intent_classification_guidance")
        # Classifies the CQR-resolved standalone_query, never the raw
        # text — a short reply like "اه" or a generic follow-up like
        # "بياخد وقت قد ايه الفحص؟" has already been resolved into a full,
        # self-contained request by the rewrite step above, so
        # classify_intent's own prompt no longer needs (and no longer
        # carries) any pronoun/history-disambiguation instructions of its
        # own — see NileChat12BBaseProvider.classify_intent's own comment.
        intent = await self.query_router_client.classify_intent(
            standalone_query, session_state.get("history", []), get_allowed_intents(), guidance=intent_guidance,
        )
        target = get_routing_target(intent)

        mode = None
        debug_json = None
        if target == RoutingTarget.TEXT_PIPELINE.value:
            # standalone_query is used ONLY for retrieval inside
            # TextReplyController; `text` itself is untouched and is what
            # reaches PATIENT MESSAGE:, chat_history, and
            # human_handoff_queue, so a short real reply like "اه" still
            # gets a naturally-phrased response, not one that talks as if
            # the patient had typed the rewritten query.
            reply_text, mode, debug_json = await self.text_reply_controller.reply(
                client_id, session_id, text, standalone_query, session_state,
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
