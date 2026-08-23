import asyncio
import json
import logging
import re

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ..GenerationInterface import GenerationInterface, GenerationTimeoutError

CHAT_COMPLETIONS_PATH = "/v1/chat/completions"

# Retries transport-level connection failures only — a reset/refused/dropped
# connection to the Colab + cloudflared quick-tunnel this provider talks to
# in practice. Real, recurring evidence: two golden-suite cases logged as
# "[REQUEST FAILED: 500 Server Error...]" and a live traceback ending in
# "ConnectionResetError: [Errno 104]" during the TLS handshake — a dropped
# tunnel, not a slow-but-alive server (that's what the existing Timeout
# handling below already covers, unretried, on purpose). status_forcelist
# is deliberately left unset: an HTTP error status FROM vLLM itself is a
# real answer from a live server, not a dropped connection, so it still
# raises via response.raise_for_status() below, unretried. allowed_methods
# explicitly includes POST — urllib3's own default excludes it (a
# non-idempotent method, in case a prior attempt partially executed
# server-side) — safe to override here specifically because
# /v1/chat/completions is a stateless generation call with no server-side
# side effects to double up. backoff_factor=0.5 keeps the 3 retries' total
# added latency to a few seconds (0.5s/1s/2s), since a reset/refused
# connection fails fast, not slow.
_CONNECTION_RETRY = Retry(
    total=3,
    connect=3,
    read=3,
    backoff_factor=0.5,
    allowed_methods=frozenset(["POST"]),
)

_JSON_OBJECT_RE = re.compile(r"\{[^{}]*\}")

# MBZUAI-Paris/Nile-Chat-12B is built on Gemma 3 (confirmed from its own
# HF model card, not assumed by analogy to Falcon-H1/Qwen's ChatML
# convention) — Gemma's chat template uses "<start_of_turn>{role}\n
# {content}<end_of_turn>\n", not "<|im_end|>". Confirmed further from the
# model's own real generation_config.json: eos_token_id=[1, 106], where
# 106 is Gemma's well-documented <end_of_turn> token id. Passed
# explicitly as `stop` for the same reason QwenProvider/FalconH1Provider
# do — a hard backstop regardless of whether the deployed server's own
# chat-template EOS wiring is exactly right.
NILE_CHAT_STOP_SEQUENCES = ["<end_of_turn>"]

# Retuned 1.0 -> 1.03 after real golden-suite evidence: the CT-Enterography
# prep reply showed a clear stuttering/repetition pattern (near-duplicate
# "يوم الميعاد، هتاكل أكل خفيف تاني..." sentences repeated across the same
# reply) — the same symptom QwenProvider originally fixed with a small
# 1.03 penalty. Deliberately NOT adopting Nile-Chat's own published
# generation_config.json default of 1.1 — this project's own history
# already showed 1.1 pushing a *different* quantized model into
# gibberish/stray-token territory, so the smaller, previously-proven-safe
# value is the more conservative first move. If stuttering persists after
# this change, that's real evidence to escalate toward 1.1 next, not a
# reason to jump there directly on Nile-Chat's own suggested default alone.
DEFAULT_REPETITION_PENALTY = 1.03


class NileChatProvider(GenerationInterface):
    """Talks to an OpenAI-compatible chat-completions endpoint serving
    MBZUAI-Paris/Nile-Chat-12B via vLLM — Phase 0 bake-off candidate 2
    (see README's Phase 0 section), the Egyptian-dialect specialist arm.
    Same adapter shape as QwenProvider/FalconH1Provider (Ports & Adapters
    — claude.md §1.2): controllers depend only on GenerationInterface, so
    this swap is a GENERATION_BACKEND config change, not a controller
    change."""

    def __init__(self, base_url: str, model_name: str, request_timeout_seconds: int = 30):
        self.base_url = base_url.rstrip("/")
        self.model_name = model_name
        self.request_timeout_seconds = request_timeout_seconds
        self.logger = logging.getLogger(__name__)
        # One Session per provider instance — this provider itself is a
        # single, process-lifetime object (see main.py's startup_span), so
        # the retry-mounted adapter and its connection pool are built once,
        # not rebuilt on every request.
        self._session = requests.Session()
        adapter = HTTPAdapter(max_retries=_CONNECTION_RETRY)
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)

    def _post_chat_completion(
        self,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
        stop: list[str] | None = NILE_CHAT_STOP_SEQUENCES,
        repetition_penalty: float = DEFAULT_REPETITION_PENALTY,
    ) -> str:
        try:
            response = self._session.post(
                f"{self.base_url}{CHAT_COMPLETIONS_PATH}",
                json={
                    "model": self.model_name,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "stop": stop,
                    "repetition_penalty": repetition_penalty,
                },
                timeout=self.request_timeout_seconds,
            )
        except requests.exceptions.Timeout as e:
            raise GenerationTimeoutError(
                f"NileChatProvider: request to {self.base_url}{CHAT_COMPLETIONS_PATH} "
                f"timed out after {self.request_timeout_seconds}s"
            ) from e
        except requests.exceptions.ConnectionError as e:
            # Reached only after _CONNECTION_RETRY's own 3 retries are
            # already exhausted inside session.post() above — a transient
            # reset/refused connection would have already succeeded on one
            # of those retries. Reusing GenerationTimeoutError rather than
            # a new exception type: the one existing caller
            # (TextReplyController._mode_a_reply) already has a real,
            # tested fallback path for it (GENERATION_UNAVAILABLE_FALLBACK)
            # — from the patient's perspective, "the backend didn't
            # respond" is the same outcome whether the cause was slow or
            # reset, so it doesn't need a second, parallel fallback path.
            raise GenerationTimeoutError(
                f"NileChatProvider: connection to {self.base_url}{CHAT_COMPLETIONS_PATH} "
                f"failed after retries ({e.__class__.__name__}: {e})"
            ) from e
        response.raise_for_status()
        payload = response.json()
        return payload["choices"][0]["message"]["content"].strip()

    async def generate_reply(
        self,
        messages: list[dict],
        temperature: float = 0.3,
        max_tokens: int = 512,
    ) -> str:
        return await asyncio.to_thread(
            self._post_chat_completion, messages, temperature, max_tokens
        )

    async def classify_intent(
        self,
        text: str,
        allowed_intents: list[str],
        history: list[dict] | None = None,
        guidance: str | None = None,
    ) -> str:
        """Same zero-shot JSON-object classification contract as
        QwenProvider/FalconH1Provider.classify_intent — model-agnostic
        prompting, intentionally identical rather than reinvented per
        candidate (see FalconH1Provider's own note on why this is a real,
        expected bake-off variable and not a bug)."""
        system_prompt_parts = [
            "You are a senior intent classifier for an Egyptian Arabic medical-services WhatsApp assistant.",
            "First, in a <reasoning>...</reasoning> block, briefly reason in 1-2 sentences about what the "
            "patient is actually asking for semantically — not which keywords appear in the message. "
            "For example, a message about insurance coverage is asking whether/what a policy covers, "
            "which is a general question about the service, not a request for a price figure.",
            "After the </reasoning> block, on a new line, output a single JSON object of the exact shape: "
            "{\"intent\": \"<one value>\"} and nothing else after it.",
            f"<one value> MUST be exactly one of: {json.dumps(allowed_intents, ensure_ascii=False)}",
            "Never invent a label outside this list. Never output the JSON object more than once, and "
            "never add any text after it.",
            "Classify ONLY the final patient message. Any conversation history given is context to "
            "resolve short/ambiguous replies (e.g. 'اه' after the assistant asked about booking an "
            "appointment means the patient's intent is book_appointment, not general_inquiry) — never "
            "classify the history itself.",
        ]
        if guidance:
            system_prompt_parts.append(guidance)
        system_prompt = "\n".join(system_prompt_parts)

        history_lines = [
            f"{'Patient' if turn.get('role') == 'user' else 'Assistant'}: {turn.get('content', '')}"
            for turn in (history or [])[-6:]
        ]
        user_content = "\n".join([
            "## Recent conversation (context only):",
            "\n".join(history_lines) if history_lines else "(no prior context)",
            "",
            "## Final patient message to classify:",
            text,
        ])

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]

        try:
            raw_response = await asyncio.to_thread(
                self._post_chat_completion, messages, 0.0, 220
            )
        except GenerationTimeoutError as e:
            self.logger.warning(f"classify_intent: request timed out ({e}) — falling back to 'unclassified'")
            return "unclassified"

        json_matches = _JSON_OBJECT_RE.findall(raw_response)
        intent = None
        if json_matches:
            try:
                parsed = json.loads(json_matches[-1])
                intent = parsed.get("intent")
            except (json.JSONDecodeError, AttributeError):
                intent = None

        if intent not in allowed_intents:
            self.logger.warning(
                f"classify_intent: model returned an out-of-set or unparseable "
                f"response {raw_response!r} — falling back to 'unclassified'"
            )
            return "unclassified"

        return intent
