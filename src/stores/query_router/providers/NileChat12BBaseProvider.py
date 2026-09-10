import asyncio
import json
import logging
import re

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ..QueryRouterInterface import QueryRouterInterface
from stores.llm.templates.template_parser import TemplateParser, TemplateBucket

CHAT_COMPLETIONS_PATH = "/v1/chat/completions"

# Same connection-retry rationale as stores/generation/providers/
# NileChatProvider.py — a reset/refused/dropped tunnel connection, not a
# slow-but-alive server (Timeout is handled separately, unretried, below).
# Duplicated here rather than imported from stores/generation/: this is a
# genuinely separate store per claude.md §1.2's Ports & Adapters
# discipline, and the two adapters' retry policies are allowed to diverge
# independently even though they happen to start identical.
_CONNECTION_RETRY = Retry(
    total=3,
    connect=3,
    read=3,
    backoff_factor=0.5,
    allowed_methods=frozenset(["POST"]),
)

# Same convention already proven across every stores/generation/
# classify_intent implementation: a <reasoning>...</reasoning> block may
# legitimately contain stray braces or the word "intent" while thinking
# out loud, so the LAST JSON object in the response is trusted, never the
# first.
_JSON_OBJECT_RE = re.compile(r"\{[^{}]*\}")

# MBZUAI-Paris/Nile-Chat-12B is built on Gemma 3 (confirmed from its own
# real generation_config.json — see stores/generation/providers/
# NileChatProvider.py's own comment, the fine-tuned sibling of this exact
# base checkpoint), chat-template convention:
# "<start_of_turn>{role}\n{content}<end_of_turn>\n".
NILE_CHAT_12B_BASE_STOP_SEQUENCES = ["<end_of_turn>"]

# Deliberately vLLM's neutral default, NOT the fine-tuned Mode A
# checkpoint's tuned 1.03 — that value was empirically retuned against a
# specific real stuttering symptom observed on THAT LoRA-adapted model
# (see NileChatProvider's own comment). This is the raw, unadapted base
# checkpoint — no equivalent evidence exists for it, and carrying a
# different model's tuned constant forward on assumption is exactly what
# this project's own history already warns against (see FalconH1Provider's
# identical reasoning for the same default choice). Retune from real
# traffic if a similar repetition artifact shows up here — never before
# then.
DEFAULT_REPETITION_PENALTY = 1.0

# MBZUAI-Paris/Nile-Chat-12B's real, published context length is 8192
# tokens (same as Mode A's own fine-tuned sibling, since it's the same
# base checkpoint) — a real, meaningful improvement over the retired 4B
# provider's tight 2048-token ceiling, which was the direct cause of a
# real production 400 (context length exceeded) earlier in this
# project's history. classify_intent/rewrite_query's own prompts are
# small regardless (see system_directives.py's whatsapp_intent_
# classification_directive / whatsapp_cqr_directive), so this ceiling is
# not expected to bind in practice, but it's no longer the fragile
# constraint it was.
_HISTORY_TURN_LIMIT = 6

# 2026-09-01: the closed intent set is exactly three values (complaint /
# inquiry / book_appointment — utils/intent_routing_map.py); "unclassified"
# was removed entirely. classify_intent's own failure fallback returns
# this constant directly — the same real value already in the allowed
# set, same safe-catch-all rationale intent_routing_map.py's own routing
# default documents (Mode A's own "decline rather than guess" behavior
# already handles a low-confidence turn gracefully). Kept as a literal
# here rather than importing Intent.INQUIRY.value from utils/ — no
# stores/ provider imports from utils/ anywhere in this codebase (Ports &
# Adapters discipline keeps this store decoupled from routing/business
# logic); `allowed_intents` is how the caller's closed set reaches this
# provider, same as it always has.
_FALLBACK_INTENT = "inquiry"

# Analytics Dashboard pipeline (2026-09-08) — classify_topic's own
# failure/out-of-set fallback. Deliberately NOT _FALLBACK_INTENT ("inquiry"
# has no meaning as a topic label) — see QueryRouterInterface.classify_
# topic's own docstring for the full reasoning behind this being a
# separate method rather than a reuse of classify_intent.
_FALLBACK_TOPIC = "unclassified"


class _RouterUnavailableError(Exception):
    """Internal-only — never crosses this module's boundary. Translates a
    request-level failure (timeout, or a dropped connection after
    _CONNECTION_RETRY's own retries are exhausted) into a single signal
    each of this provider's methods' own try/except already handles,
    matching QueryRouterInterface's documented never-raise contract.
    Deliberately not GenerationInterface.GenerationTimeoutError — this
    store doesn't depend on stores/generation/'s internals, per
    claude.md §1.2."""


def _extract_last_json_object(raw_response: str) -> dict | None:
    """Shared by both classify_intent and rewrite_query: a <reasoning>
    block may legitimately contain stray braces, so the LAST JSON object
    in the response is trusted, never the first. Returns None on any
    parse failure — callers decide their own safe-default fallback."""
    json_matches = _JSON_OBJECT_RE.findall(raw_response)
    if not json_matches:
        return None
    try:
        parsed = json.loads(json_matches[-1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


class NileChat12BBaseProvider(QueryRouterInterface):
    """Talks to an OpenAI-compatible chat-completions endpoint serving
    the RAW, unadapted MBZUAI-Paris/Nile-Chat-12B checkpoint via vLLM —
    the dedicated query-rewriting and intent-classification sidecar.
    Deliberately the BASE checkpoint, never the fine-tuned Mode A
    deployment (stores/generation/providers/NileChatProvider.py) — same
    underlying weights, but without the LoRA adapter Mode A was trained
    with, so it carries none of that adapter's narrow behavioral shape.

    2026-09-01: replaces the earlier NileChat4BProvider entirely (deleted,
    not kept alongside this one). Real production evidence — four
    separate prompt-engineering attempts (single combined prompt, split
    calls with concrete examples, split calls with abstract placeholders,
    CQR-first reordering) all failed the exact same generic-reference
    anaphora-resolution pattern on that 4B checkpoint, confirmed via raw-
    response logging to be a genuine reasoning failure (valid JSON,
    content mindlessly copy-pasted from the input), not a parsing or
    prompt-formatting bug — showed a real reliability ceiling specific to
    that checkpoint's size/capability on this task, not something further
    prompt iteration was likely to fix. See QueryRouterInterface's own
    docstring for the full history.

    This choice is a genuine architectural bet, not a proven fix: general
    LLM-scaling trends make a 3x larger, general-purpose model a credible
    candidate for a task requiring reliable multi-step reasoning over
    conversation history, but that trend hasn't been verified against
    THIS project's own real traffic for THIS specific task yet — same
    "strong evidence-informed bet, not a guarantee" status the 4B
    provider itself started with. Real multi-turn Postman traffic is
    what confirms or rejects it, not the size argument alone.

    Ports & Adapters shape (claude.md §1.2) unchanged from its
    predecessor: IntentRoutingController depends only on
    QueryRouterInterface, so this swap was a QUERY_ROUTER_BACKEND config
    change plus this one new providers/ file — no controller edit."""

    def __init__(self, base_url: str, model_name: str, request_timeout_seconds: int = 15):
        self.base_url = base_url.rstrip("/")
        self.model_name = model_name
        self.request_timeout_seconds = request_timeout_seconds
        self.logger = logging.getLogger(__name__)
        # This provider's own fixed protocol directives
        # (whatsapp_intent_classification_directive, whatsapp_cqr_directive)
        # live in Bucket C, same as every other LLM instruction in this
        # project — resolved here, not hardcoded as Python strings.
        # `guidance` (the domain-specific, per-call content) is still
        # resolved by the caller (IntentRoutingController) and passed in.
        self._template_parser = TemplateParser()
        # One Session per provider instance — this provider itself is a
        # single, process-lifetime object (see main.py's startup_span), so
        # the retry-mounted adapter and its connection pool are built once,
        # not rebuilt on every request.
        self._session = requests.Session()
        adapter = HTTPAdapter(max_retries=_CONNECTION_RETRY)
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)

    def _post_chat_completion(self, messages: list[dict], temperature: float, max_tokens: int) -> str:
        request_payload = {
            "model": self.model_name,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stop": NILE_CHAT_12B_BASE_STOP_SEQUENCES,
            "repetition_penalty": DEFAULT_REPETITION_PENALTY,
        }
        try:
            response = self._session.post(
                f"{self.base_url}{CHAT_COMPLETIONS_PATH}",
                json=request_payload,
                timeout=self.request_timeout_seconds,
            )
        except requests.exceptions.Timeout as e:
            raise _RouterUnavailableError(
                f"NileChat12BBaseProvider: request to {self.base_url}{CHAT_COMPLETIONS_PATH} "
                f"timed out after {self.request_timeout_seconds}s"
            ) from e
        except requests.exceptions.ConnectionError as e:
            # Reached only after _CONNECTION_RETRY's own 3 retries are
            # already exhausted inside session.post() above — same
            # real-world justification as NileChatProvider's identical
            # handling (a dropped tunnel, not a slow-but-alive server).
            raise _RouterUnavailableError(
                f"NileChat12BBaseProvider: connection to {self.base_url}{CHAT_COMPLETIONS_PATH} "
                f"failed after retries ({e.__class__.__name__}: {e})"
            ) from e
        try:
            response.raise_for_status()
        except requests.exceptions.HTTPError as e:
            self.logger.error(
                f"NileChat12BBaseProvider: {response.status_code} error from "
                f"{self.base_url}{CHAT_COMPLETIONS_PATH}\n"
                f"--- payload sent ---\n{json.dumps(request_payload, ensure_ascii=False, indent=2)}\n"
                f"--- response body ---\n{response.text}"
            )
            raise
        payload = response.json()
        return payload["choices"][0]["message"]["content"].strip()

    def _format_history(self, history: list[dict]) -> str:
        history_lines = [
            f"{'Patient' if turn.get('role') == 'user' else 'Assistant'}: {turn.get('content', '')}"
            for turn in (history or [])[-_HISTORY_TURN_LIMIT:]
        ]
        return "\n".join(history_lines) if history_lines else "(no prior context)"

    async def classify_intent(
        self,
        text: str,
        history: list[dict],
        allowed_intents: list[str],
        guidance: str | None = None,
    ) -> str:
        # `text` (CQR reordering) has ALREADY been rewritten into a
        # standalone, self-contained query by rewrite_query(), called
        # first by IntentRoutingController — never the patient's raw
        # message — which is why whatsapp_intent_classification_directive
        # carries no pronoun/history-resolution instructions of its own.
        system_prompt_parts = [
            self._template_parser.resolve(TemplateBucket.C, "whatsapp_intent_classification_directive"),
            f"<one value> MUST be exactly one of: {json.dumps(allowed_intents, ensure_ascii=False)}",
        ]
        if guidance:
            system_prompt_parts.append(guidance)
        system_prompt = "\n".join(system_prompt_parts)

        user_content = "\n".join([
            "## Recent conversation (context only):",
            self._format_history(history),
            "",
            "## Message to classify (already standalone):",
            text,
        ])

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]

        try:
            raw_response = await asyncio.to_thread(self._post_chat_completion, messages, 0.0, 150)
        except _RouterUnavailableError as e:
            self.logger.warning(f"classify_intent: request failed ({e}) — falling back to '{_FALLBACK_INTENT}'")
            return _FALLBACK_INTENT

        parsed = _extract_last_json_object(raw_response)
        intent = parsed.get("intent") if parsed else None

        if intent not in allowed_intents:
            self.logger.warning(
                f"classify_intent: model returned an out-of-set or unparseable intent in "
                f"{raw_response!r} — falling back to '{_FALLBACK_INTENT}'"
            )
            intent = _FALLBACK_INTENT

        return intent

    async def rewrite_query(
        self,
        text: str,
        history: list[dict],
        guidance: str | None = None,
    ) -> str:
        """Contextual Query Reformulation (CQR) — the FIRST call
        IntentRoutingController.route_turn makes on every turn,
        unconditionally, before intent is even known. Its own prompt is
        deliberately generic/single-purpose (resolve references, never
        answer the question) — it never needs to know or care what the
        eventual intent will be, which is exactly why the reordering
        works: classify_intent (above) can always assume its own input
        is already standalone."""
        system_prompt_parts = [
            self._template_parser.resolve(TemplateBucket.C, "whatsapp_cqr_directive"),
        ]
        if guidance:
            system_prompt_parts.append(guidance)
        system_prompt = "\n".join(system_prompt_parts)

        user_content = "\n".join([
            "## Recent conversation (context only):",
            self._format_history(history),
            "",
            "## Final patient message to rewrite:",
            text,
        ])

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]

        try:
            raw_response = await asyncio.to_thread(self._post_chat_completion, messages, 0.0, 180)
        except _RouterUnavailableError as e:
            self.logger.warning(
                f"rewrite_query: request failed ({e}) — falling back to raw text {text!r}"
            )
            return text

        parsed = _extract_last_json_object(raw_response)
        resolved_query = parsed.get("resolved_query") if parsed else None

        if not resolved_query or not isinstance(resolved_query, str) or not resolved_query.strip():
            self.logger.warning(
                f"rewrite_query: resolved_query missing/empty in model response "
                f"{raw_response!r} — falling back to raw text {text!r}"
            )
            return text

        return resolved_query

    async def classify_topic(
        self,
        text: str,
        allowed_topics: list[str],
        guidance: str | None = None,
    ) -> str:
        """See QueryRouterInterface.classify_topic's own docstring for the
        full contract. Structurally the same shape as classify_intent
        above (closed-set JSON classification, last-JSON-object parsing,
        never raises) with its own dedicated fallback and no `history`."""
        effective_allowed = allowed_topics + [_FALLBACK_TOPIC] if _FALLBACK_TOPIC not in allowed_topics else allowed_topics

        system_prompt_parts = [
            self._template_parser.resolve(TemplateBucket.C, "whatsapp_topic_classification_directive"),
            f"<one value> MUST be exactly one of: {json.dumps(effective_allowed, ensure_ascii=False)}",
        ]
        if guidance:
            system_prompt_parts.append(guidance)
        system_prompt = "\n".join(system_prompt_parts)

        user_content = f"## Message to classify:\n{text}"

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]

        try:
            raw_response = await asyncio.to_thread(self._post_chat_completion, messages, 0.0, 150)
        except _RouterUnavailableError as e:
            self.logger.warning(f"classify_topic: request failed ({e}) — falling back to {_FALLBACK_TOPIC!r}")
            return _FALLBACK_TOPIC

        parsed = _extract_last_json_object(raw_response)
        topic = parsed.get("topic") if parsed else None

        if topic not in effective_allowed:
            self.logger.warning(
                f"classify_topic: model returned an out-of-set or unparseable topic in "
                f"{raw_response!r} — falling back to {_FALLBACK_TOPIC!r}"
            )
            topic = _FALLBACK_TOPIC

        return topic
