# ADR-0010: Cache the conversation prefix; compact only as a safety valve; cap tokens per run

- **Status:** Accepted. Built in the PoC.
- **Date:** 2026-09-30

## Context

The agent loop re-sends the whole conversation on every LLM call, so input tokens grow with the square of the step count. A design review found that the capacity model assumed ~40k input tokens per run without measuring it, and that nothing bounded a run's spend except the step cap and deadline. At 5-10 incidents per minute, per-run token cost drives both the bill and the provider rate limit.

`asap tokens` measures the prompt each call would carry. For the three scenarios: 11-14 calls, 6.0-6.5k peak context, 48-66k input tokens processed per run. For a worst case of 15 max-size log and trace results: 9.7k peak, 101k processed. The fixed floor is the system prompt plus tool schemas, ~4.4k of the ~6k investigation context.

## Options considered

| Option | For | Against |
|---|---|---|
| Rolling-window compaction every turn | Smallest context each call | Rewrites history every turn, so the prompt cache never hits; highest cost |
| Early threshold compaction (6k) | Bounds context; measured 83k processed in the worst case, down from 101k | Each compaction invalidates the cached prefix; measured cost-equivalent **38k vs 22k** without it |
| **Cache the whole conversation prefix; compact only above a high ceiling (16k); hard per-run budget** | Typical runs cost ~19-22k cost-equivalent; context stays bounded for runaway runs; spend is capped | Contexts are larger than strictly necessary; depends on the provider's prompt caching |
| Stateless calls (re-summarise evidence into a fresh prompt each turn) | Fixed context size | Loses the model's own reasoning trail; the summariser is another model call and another failure mode |

## Decision

- The Anthropic adapter places cache breakpoints on the tool schemas, the system prompt and the last conversation block, so each turn reads the previous turn from cache.
- Compaction replaces all but the 3 most recent tool results with one-line digests that keep their evidence IDs, only when the context passes 16k tokens. It runs as a batch, so the prefix is stable between compactions. Old evidence stays citable; the full results remain in run state.
- A per-run budget of 120k processed input tokens is checked **before** each call against the projected spend. Exceeding it ends the run as REPORT_ONLY with the evidence gathered so far. Budget accounting uses the provider's usage (uncached + cache reads + cache writes) when reported, and the estimate otherwise.

## Consequences

- Typical runs never compact; the worst measured run peaks at 9.7k and stays under the budget.
- The capacity model in SYSTEM_DESIGN.md now uses measured numbers: ~0.5-0.7M processed input tokens per minute at 10 incidents/min, ~0.2M cost-equivalent.
- The estimates come from the deterministic reasoner. A live model writes longer `reasoning` fields and may take more steps; its exact usage is recorded per call in the audit log and should replace these numbers once live runs exist.
- The next lever is the schema floor: exposing fewer tools per phase would shrink every call.

## Revisit if

Live-model runs routinely approach the budget or the compaction ceiling, or the chosen provider's caching becomes unavailable or changes its pricing. In that case, move to per-phase tool sets and evidence-by-reference prompts.
