import asyncio
import json
import logging
import re

import requests

from ..GenerationInterface import GenerationInterface, GenerationTimeoutError

CHAT_COMPLETIONS_PATH = "/v1/chat/completions"

_JSON_OBJECT_RE = re.compile(r"\{[^{}]*\}")

# tiiuae/Falcon-H1-34B-Instruct's own published chat template uses the
# same ChatML convention as Qwen2.5 —
# "<|im_start|>{role}\n{content}<|im_end|>\n" — confirmed from the model's
# real tokenizer/chat_template, not assumed by copying QwenProvider's
# value. A different Phase 0 candidate must not inherit this without the
# same confirmation (see NileChatProvider, still pending its own check).
FALCON_H1_STOP_SEQUENCES = ["<|im_end|>"]

# Deliberately left at vLLM's neutral default (no penalty) rather than
# reusing QwenProvider's DEFAULT_REPETITION_PENALTY=1.03 — that value was
# empirically tuned against Qwen2.5-7B's own quantized stuttering
# behavior (see QwenProvider's comment) and has no evidence it transfers
# to a different model/checkpoint. Retune from real golden-suite output
# the same way 1.03 was arrived at, if a similar repetition artifact
# shows up here — never carry a prior model's tuned constant forward on
# assumption.
DEFAULT_REPETITION_PENALTY = 1.0


class FalconH1Provider(GenerationInterface):
    """Talks to an OpenAI-compatible chat-completions endpoint serving
    tiiuae/Falcon-H1-34B-Instruct via vLLM — Phase 0 bake-off candidate 1
    (see README's Phase 0 section). Same adapter shape as QwenProvider
    (Ports & Adapters — claude.md §1.2): controllers depend only on
    GenerationInterface, so this swap is a GENERATION_BACKEND config
    change, not a controller change."""

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
        stop: list[str] | None = FALCON_H1_STOP_SEQUENCES,
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
            # Root-caused against real golden-suite traffic: 5/67 turns
            # failed this way against a --enforce-eager, Colab-tunneled
            # 34B deployment (4 as an outer-client timeout after two
            # slow-but-individually-under-budget LLM calls, 1 as this
            # provider's own call individually crossing the timeout and
            # previously surfacing as a raw 500). Translated here, the
            # one place this provider talks HTTP, so both call sites
            # below inherit it without duplicating the try/except.
            raise GenerationTimeoutError(
                f"FalconH1Provider: request to {self.base_url}{CHAT_COMPLETIONS_PATH} "
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
        QwenProvider.classify_intent — the prompting strategy itself is
        model-agnostic (it only relies on general instruction-following,
        not a Qwen-specific behavior), so it is intentionally identical
        rather than reinvented per candidate. Bake-off note: this means
        swapping GENERATION_BACKEND also swaps which model performs
        intent routing, not just reply phrasing — a difference here can
        change which retrieval path a query takes before generation ever
        happens, which is a real, expected variable in the comparison,
        not a bug."""
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
            # classify_intent's contract (GenerationInterface) is to
            # never raise — a timeout is just another way the model
            # failed to produce a usable label, same bucket as
            # unparseable JSON below.
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
