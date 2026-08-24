from uuid import UUID
from typing import Any, Literal, Optional

from pydantic import BaseModel


class ChatRequest(BaseModel):
    session_id: UUID
    message: str
    # Frontend brand-filter selector. None (omitted) means "no change this
    # turn" — the session keeps whatever brand_filter it already had
    # (utils/session_store.py's own persisted state), so the frontend
    # doesn't have to resend the current selection on every message.
    # "all" is an explicit clear, distinct from omitting the field —
    # session_state["brand_filter"] itself is None for "all", but a
    # patient actively switching back to "All" needs its own signal,
    # not just silence.
    brand_filter: Optional[Literal["technoscan", "cairoscan", "all"]] = None


class ChatResponse(BaseModel):
    client_id: str
    session_id: UUID
    reply: str
    intent: str
    mode: Optional[str] = None
    # The fine-tuned model's own extracted JSON block for this turn (see
    # TextReplyController.reply's docstring) — always None for Mode B,
    # the out-of-domain decline path, or a not-yet-fine-tuned model still
    # answering in plain text. Internal/testing use only (currently
    # scripts/collect_golden_responses.py's grounding check) — never the
    # patient-facing content, which stays entirely in `reply`. Typed Any
    # rather than a fixed schema because the real shape varies: a dict
    # for a narrow-breadth turn, a list of per-source dicts for a broad
    # one — see grounding_gate.py's own "narrow" vs "broad" check split
    # for why these two shapes are legitimately different, not a bug.
    debug_json: Optional[Any] = None
