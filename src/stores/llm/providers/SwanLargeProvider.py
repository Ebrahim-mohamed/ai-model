import asyncio

from ..LLMInterface import LLMInterface


class SwanLargeUnavailableError(Exception):
    """Raised the first time this provider actually tries to load
    UBC-NLP/swan-large. Two real blockers were found while implementing
    Step 8 (see README's Step 8 section for the full research):

      1. huggingface.co/UBC-NLP/swan-large returns 401 Unauthorized on
         both the model page and its API — this repo is almost certainly
         gated and needs a HuggingFace account, accepting its license,
         and a valid HF token before it can be downloaded at all.
      2. Swan-Large is built on a 7B-parameter ArMistral backbone (not a
         lightweight BERT-style encoder like BGE-M3) — it needs roughly
         14GB+ of VRAM/RAM just to load in fp16. This dev machine's GPU
         (Quadro M2200, 4GB VRAM) and WSL2's RAM allocation (3.7GB) are
         both far short of that, independent of the gating issue.

    This class exists so LLMInterface/LLMEnums/LLMProviderFactory's
    contract is complete and a real Swan-Large can be swapped in later
    with a config change, not a rewrite — but it fails loudly here rather
    than fabricating output, per claude.md's no-synthetic-data discipline
    extended to models as well as business data."""


class SwanLargeProvider(LLMInterface):
    """UBC-NLP/swan-large — currently unable to run in this environment;
    see SwanLargeUnavailableError above for why. QUERY_PREFIX is left
    UNCONFIRMED (empty) rather than guessed — the proposal itself (Step 4)
    explicitly warns against assuming BGE-M3's prefix convention applies
    here without checking the real model card, which requires the gated
    access this environment doesn't have."""

    QUERY_PREFIX = ""  # UNCONFIRMED — do not assume BGE-M3's convention applies.

    _model = None

    def __init__(self, hf_token: str | None = None):
        self.hf_token = hf_token

    def _get_model(self):
        if SwanLargeProvider._model is None:
            try:
                from sentence_transformers import SentenceTransformer
                SwanLargeProvider._model = SentenceTransformer(
                    "UBC-NLP/swan-large", token=self.hf_token,
                )
            except Exception as e:
                raise SwanLargeUnavailableError(
                    "Could not load UBC-NLP/swan-large. Most likely cause: "
                    "gated-repo access not yet granted on HuggingFace, or "
                    "HF_TOKEN unset/invalid — or this machine's VRAM/RAM is "
                    "insufficient for a 7B-parameter model. See README's "
                    f"Step 8 section for details. Original error: {e}"
                ) from e
        return SwanLargeProvider._model

    @property
    def embedding_dimension(self) -> int:
        raise SwanLargeUnavailableError(
            "Swan-Large's real embedding dimension is unconfirmed — reading "
            "it requires gated HuggingFace access this environment doesn't "
            "have. Never guess this value; confirm it from the real model "
            "config before using this provider for anything beyond "
            "interface wiring."
        )

    async def embed_text(self, texts: list[str], is_query: bool = False) -> list[list[float]]:
        prepared = [f"{self.QUERY_PREFIX}{t}" for t in texts] if (is_query and self.QUERY_PREFIX) else list(texts)
        return await asyncio.to_thread(self._encode, prepared)

    def _encode(self, texts: list[str]) -> list[list[float]]:
        model = self._get_model()
        vectors = model.encode(texts, batch_size=32, normalize_embeddings=True, show_progress_bar=False)
        return vectors.tolist()
