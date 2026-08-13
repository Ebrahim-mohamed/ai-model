import json

import redis.asyncio as redis

DEFAULT_STATE = {
    "dialogue_state": None,   # e.g. "greeting", "closing" — see dialogue_state_template_map
    "brand_filter": None,     # "cairoscan" | "technoscan" | None (= all)
    "slots": {},              # Step 5's booking slot-fill object lives here later
    "history": [],            # [{"role": "user"|"assistant", "content": str}, ...]
}


def _session_key(client_id: str, session_id) -> str:
    return f"session:{client_id}:{session_id}"


class SessionStore:
    """Tier 1 (Redis) of Section 3 Step 1's two-tier chat-history
    architecture — rolling-expiry working memory. Every write resets the
    TTL (`SESSION_TTL_SECONDS`), so an active conversation never expires
    mid-flow but an abandoned one doesn't accumulate indefinitely
    (claude.md §6, Implementation Plan Step 1). Tier 2 (`ChatHistoryModel`
    / Postgres `chat_history`) is the permanent backing store this class
    hydrates from on a cache miss — never the other way around."""

    def __init__(self, redis_url: str, ttl_seconds: int, history_window: int):
        self._redis = redis.from_url(redis_url, decode_responses=True)
        self.ttl_seconds = ttl_seconds
        self.history_window = history_window

    async def get_session(self, client_id: str, session_id) -> dict | None:
        raw = await self._redis.get(_session_key(client_id, session_id))
        if raw is None:
            return None
        return json.loads(raw)

    async def save_session(self, client_id: str, session_id, state: dict) -> None:
        await self._redis.set(
            _session_key(client_id, session_id),
            json.dumps(state, ensure_ascii=False, default=str),
            ex=self.ttl_seconds,
        )

    async def get_or_hydrate_session(self, client_id: str, session_id, chat_history_model) -> dict:
        """The Implementation Plan's hydration mechanism: a Tier-1 hit
        returns as-is (and its TTL is not touched here — only a write
        extends it, matching the "rolling expiry on activity" design, not
        every read). A miss rebuilds a fresh session from Tier 2's last
        `history_window` messages, persists it back to Tier 1, and
        returns it — so a patient returning after their Redis session
        expired resumes with real prior context, not a blank slate."""
        existing = await self.get_session(client_id, session_id)
        if existing is not None:
            return existing

        recent_messages = await chat_history_model.get_recent_messages(
            client_id=client_id, session_id=session_id, limit=self.history_window,
        )

        state = dict(DEFAULT_STATE)
        state["history"] = [
            {"role": "user" if message.direction == "inbound" else "assistant", "content": message.content}
            for message in recent_messages
        ]

        await self.save_session(client_id, session_id, state)
        return state

    async def close(self) -> None:
        await self._redis.aclose()
