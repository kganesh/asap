# ADR-0010: Cache the conversation prefix; compact only as a safety valve; cap tokens per run

- **Status:** Accepted. Built in the PoC.
- **Date:** 2026-09-30

## Context

The agent loop re-sends the whole conversation on every LLM call, so input tokens grow with the square of the step count. A design review found that the capacity model assumed ~40k input tokens per run without measuring it, and that nothing bounded a run's spend except the step cap and deadline. At 5-10 incidents per minute, per-run token cost drives both the bill and the provider rate limit.

`asap tokens` measures the prompt each call would carry; its estimator is calibrated against a live Claude Sonnet 5.5 run (JSON-heavy prompts: ~2.5 characters per token). Scripted scenarios: 11-14 calls, 9.6-10.5k peak context, 77-105k input tokens processed. Worst case of 15 max-size log and trace results: 15.6k peak, 162k processed. The live `bad_deploy` run: 7 calls, 47.5k processed, 59% of it cache reads. The fixed floor is the system prompt plus tool schemas.

## Options considered

| Option | For | Against |
|---|---|---|
| Rolling-window compaction every turn | Smallest context each call | Rewrites history every turn, so the prompt cache never hits; highest cost |
| Early threshold compaction (6k) | Bounds context; measured 129k processed in the worst case, down from 162k | Each compaction invalidates the cached prefix; measured cost-equivalent **76k vs 36k** without it |
| **Cache the whole conversation prefix; compact only above a high ceiling (16k); hard per-run budget** | Typical runs cost ~27-35k cost-equivalent; context stays bounded for runaway runs; spend is capped | Contexts are larger than strictly necessary; depends on the provider's prompt caching |
| Stateless calls (re-summarise evidence into a fresh prompt each turn) | Fixed context size | Loses the model's own reasoning trail; the summariser is another model call and another failure mode |

## Decision

- The Anthropic adapter places cache breakpoints on the tool schemas, the system prompt and the last conversation block, so each turn reads the previous turn from cache.
- Compaction replaces all but the 3 most recent tool results with one-line digests that keep their evidence IDs, only when the context passes 16k tokens. It runs as a batch, so the prefix is stable between compactions. Old evidence stays citable; the full results remain in run state.
- A per-run budget of 200k processed input tokens (about 2x a typical run) is checked **before** each call against the projected spend. Exceeding it ends the run as REPORT_ONLY with the evidence gathered so far. Budget accounting uses the provider's usage (uncached + cache reads + cache writes) when reported, and the estimate otherwise.

## Consequences

- Typical runs never compact; the worst measured run peaks at 15.6k and stays under the budget.
- The capacity model in SYSTEM_DESIGN.md now uses a live run plus calibrated estimates: ~0.5-1M processed input tokens per minute at 10 incidents/min, ~0.3M cost-equivalent.
- The live run showed most cache writes happen when the tool set changes between phases. The next experiment is one constant tool set across phases (the orchestrator already enforces per-phase tools), traded against a slightly larger PLAN call.

## Revisit if

Live-model runs routinely approach the budget or the compaction ceiling, or the chosen provider's caching becomes unavailable or changes its pricing. In that case, move to per-phase tool sets and evidence-by-reference prompts.
