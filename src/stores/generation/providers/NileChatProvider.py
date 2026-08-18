import asyncio
import json
import logging
import re

import requests

from ..GenerationInterface import GenerationInterface, GenerationTimeoutError

CHAT_COMPLETIONS_PATH = "/v1/chat/completions"

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

    def _post_chat_completion(
        self,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
        stop: list[str] | None = NILE_CHAT_STOP_SEQUENCES,
        repetition_penalty: float = DEFAULT_REPETITION_PENALTY,
    ) -> str:
        try:
            response = requests.post(
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
