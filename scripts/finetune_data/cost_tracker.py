"""Token accounting for the fine-tuning data-generation pipeline —
mirrors llm_finetuning.ipynb's cost-tracking loop (cell-31/32): running
prompt/completion token totals, a periodic mid-run print, and a final
per-pass + total cost report. Claude pricing (not the notebook's own
gpt-4o-mini rates), since this project's teacher model is Claude Opus 5
via the Anthropic API — see this session's teacher-model evaluation for
why (Structured/strict JSON enforcement, adaptive-thinking judgment on
the Absence/Ambiguous categories, and a non-differentiating cost at this
dataset's volume).

Batch pricing (50% off standard), since sampling.py's full-run path now
submits every pass through the Message Batches API (teacher.submit_batch)
rather than one live call at a time — real per-example cost measured on
the live synchronous path ($0.022/row, ~45 rows/$1) was double this
before that switch, matching the standard-vs-batch rate ratio exactly."""

from dataclasses import dataclass

# Claude Opus 5 BATCH pricing, $ per 1M tokens — the one place this rate
# is defined (claude.md §1.3). Update here, not at call sites, if the
# teacher model or pricing tier ever changes. The `--limit` smoke-test
# path still uses the live (non-batch) call and therefore actually costs
# 2x this per token — an acceptable, disclosed tradeoff for fast
# iteration on a handful of rows, never for a full run.
PRICE_PER_1M_INPUT_TOKENS = 2.50
PRICE_PER_1M_OUTPUT_TOKENS = 12.50

PRINT_EVERY_N_CALLS = 3  # same cadence as the notebook's `if (ix % 3) == 0`


@dataclass
class PassTotals:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def cost(self) -> float:
        return (
            (self.prompt_tokens / 1_000_000) * PRICE_PER_1M_INPUT_TOKENS
            + (self.completion_tokens / 1_000_000) * PRICE_PER_1M_OUTPUT_TOKENS
        )


class CostTracker:
    """One instance shared across all four passes (Stages 1-4). Each pass
    records its calls under its own label, so a per-pass cost/volume
    breakdown is always available in the final report, but the running
    total printed mid-run is the grand total across every pass — matching
    the notebook's single running prompt_tokens/completion_tokens
    accumulator, just re-derived per print instead of kept as a second
    parallel counter."""

    def __init__(self):
        self._by_pass: dict[str, PassTotals] = {}
        self._total_calls = 0

    def record(self, pass_label: str, prompt_tokens: int, completion_tokens: int) -> None:
        totals = self._by_pass.setdefault(pass_label, PassTotals())
        totals.calls += 1
        totals.prompt_tokens += prompt_tokens
        totals.completion_tokens += completion_tokens
        self._total_calls += 1

        if self._total_calls % PRINT_EVERY_N_CALLS == 0:
            grand_total = self.grand_total()
            print(f"[cost] {self._total_calls} teacher calls so far — running cost: ${grand_total.cost():.4f}")

    def grand_total(self) -> PassTotals:
        combined = PassTotals()
        for totals in self._by_pass.values():
            combined.calls += totals.calls
            combined.prompt_tokens += totals.prompt_tokens
            combined.completion_tokens += totals.completion_tokens
        return combined

    def final_report(self) -> None:
        print("\n" + "=" * 64)
        print("Teacher distillation cost report")
        print("=" * 64)
        for pass_label, totals in self._by_pass.items():
            print(
                f"  {pass_label:20s} calls={totals.calls:5d}  "
                f"input_tokens={totals.prompt_tokens:8d}  "
                f"output_tokens={totals.completion_tokens:8d}  "
                f"cost=${totals.cost():.4f}"
            )
        grand_total = self.grand_total()
        print("-" * 64)
        print(
            f"  {'TOTAL':20s} calls={grand_total.calls:5d}  "
            f"input_tokens={grand_total.prompt_tokens:8d}  "
            f"output_tokens={grand_total.completion_tokens:8d}  "
            f"cost=${grand_total.cost():.4f}"
        )
        print("=" * 64 + "\n")
