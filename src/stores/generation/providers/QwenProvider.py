import asyncio
import json
import logging
import re

import requests

from ..GenerationInterface import GenerationInterface, GenerationTimeoutError

CHAT_COMPLETIONS_PATH = "/v1/chat/completions"

# Extracts the final {"intent": "..."} JSON object out of a response that
# now also carries a <reasoning>...</reasoning> block ahead of it (see
# classify_intent) — non-greedy, and searched for the LAST match in the
# response rather than the first, since the reasoning text itself may
# legitimately contain the word "intent" or stray braces while thinking
# out loud, but the model is instructed to always emit the real JSON
# object last.
_JSON_OBJECT_RE = re.compile(r"\{[^{}]*\}")

# Qwen2.5-Instruct's own chat-template turn-end marker — a model/deployment
# constant, the same kind of thing CrossEncoderProvider's MODEL_NAME or
# MSALGraphProvider's authority/scope constants are: it belongs to this
# specific vendor adapter, not to a per-client business threshold, so it
# has no business living in client_config. Passed explicitly as `stop`
# rather than trusted to the server's own EOS handling: real test traffic
# showed generations that answered correctly and then kept sampling past
# where they should have stopped, degrading into repetition and — for
# this specific base model, heavily Chinese-pretrained — Chinese-script
# text. An explicit stop sequence is a hard backstop for that regardless
# of whether the deployed chat template's own EOS wiring is exactly right.
QWEN_STOP_SEQUENCES = ["<|im_end|>"]

# Re-introduced at a much smaller value after 1.1 was tried and reverted
# (see _post_chat_completion below) — 1.1 was aggressive enough to push
# this quantized model's sampling into unmapped territory (gibberish,
# stray non-Arabic tokens); real traffic without any repetition_penalty
# then surfaced its own real failure mode instead — short-phrase
# stuttering (e.g. "تانية تانية"). A micro-value discourages the exact
# kind of immediate-adjacent-token repeat that causes stuttering without
# meaningfully reshaping the rest of the distribution the way 1.1 did.
DEFAULT_REPETITION_PENALTY = 1.03


class QwenProvider(GenerationInterface):
    """Talks to an OpenAI-compatible chat-completions endpoint serving
    Qwen2.5-7B-Instruct (a self-hosted vLLM instance per the Implementation
    Plan's Deployment & Infrastructure section — `base_url` is wherever
    that's actually running, never assumed to be localhost). `requests` is
    synchronous; every call is offloaded via asyncio.to_thread, the same
    pattern MSALGraphProvider already uses, so a slow generation call
    never stalls the event loop for other tenants."""

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
        stop: list[str] | None = QWEN_STOP_SEQUENCES,
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
            # Translated here, the one place this provider talks HTTP, so
            # both call sites below (generate_reply, classify_intent)
            # inherit it without duplicating the try/except — each then
            # decides independently what a timeout means for its own
            # contract (see their own docstrings/GenerationTimeoutError).
            raise GenerationTimeoutError(
                f"QwenProvider: request to {self.base_url}{CHAT_COMPLETIONS_PATH} "
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
        """Zero-shot closed-set classification via the same chat-
        completions endpoint, temperature=0 for deterministic labeling.
        The response is validated against `allowed_intents` before being
        trusted — a model that answers outside the closed set (or fails
        to answer parseable JSON at all) never propagates an invalid
        label downstream; it falls back to whichever entry
        utils/intent_routing_map.py designates as the default/unclassified
        route, exactly like Phase 4's complaint classifier will.

        `history` is rendered as a plain-text transcript embedded in the
        user turn, not as literal prior chat turns — this keeps the
        strict single-JSON-object output contract intact (replaying real
        assistant/user turns into a structured-output call risks the
        model trying to *continue the conversation* instead of emitting
        the classification), while still giving the classifier enough
        context to resolve a short reply like "اه" against whatever the
        assistant just asked.

        `guidance`, when supplied, is appended to the system prompt as-is
        — real worked examples resolve ambiguous real-world cases (a
        broad "what do you offer" question, an out-of-scope medical
        question) far more reliably than the bare closed-set list alone.

        The model is asked for a short chain-of-thought <reasoning> block
        before the JSON — real testing showed the bare "just output JSON"
        contract collapsing to a shallow keyword match (e.g. "تأمين بتاعي
        بيغطي ايه؟" landing on query_price because of a coincidental
        overlap with a pricing example) with no way to steer it further
        via examples alone. Forcing an explicit semantic-intent step first
        gives the model room to actually reason about what the patient
        wants before committing to a label. The JSON is still parsed out
        of the tail of the response (_JSON_OBJECT_RE), so this doesn't
        change the strict-single-label contract callers depend on."""
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

        # max_tokens raised from the pre-CoT 64: the <reasoning> block
        # needs real room ahead of the JSON object, and a truncated
        # reasoning block would otherwise cut off before the JSON ever
        # gets emitted.
        try:
            raw_response = await asyncio.to_thread(
                self._post_chat_completion, messages, 0.0, 220
            )
        except GenerationTimeoutError as e:
            # classify_intent's own contract (see GenerationInterface) is
            # to never raise — a timeout is just another way the model
            # failed to produce a usable label, same bucket as unparseable
            # JSON below. Unlike generate_reply, no patient-facing text is
            # decided here, so there's nothing a caller needs to catch.
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
