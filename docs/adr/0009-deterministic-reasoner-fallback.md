# ADR-0009: A deterministic reasoner when no LLM key is configured

- **Status:** Accepted. Built in the PoC.
- **Date:** 2026-09-29

## Context

Reviewers must be able to run the PoC with one command, and many won't have (or won't want to use) an API key. The assignment also requires an LLM-based agentic workflow, and a demo that silently depends on a live model is flaky in CI.

## Options considered

| Option | For | Against |
|---|---|---|
| Require an API key | Most authentic | Fails for anyone without a key; nondeterministic CI |
| Replay recorded LLM responses (cassettes) | Real model output, deterministic | Breaks on any change to prompts, tools or telemetry; hides whether the logic generalizes |
| **Rule-based reasoner behind the same LLM interface, with the real LLM used automatically when a key is present** | Always runs; deterministic tests; exercises every guardrail through the same gateway, control plane and executor | Its diagnoses come from explicit rules, not model reasoning, so it proves the guardrails and plumbing rather than LLM diagnostic quality |

## Decision

`--llm auto` selects Claude when `ANTHROPIC_API_KEY` is set, an OpenAI-compatible endpoint (including local Ollama) when `OPENAI_BASE_URL` or `OPENAI_API_KEY` is set, and otherwise the `DeterministicReasoner`. The console states which one is running.

The reasoner is **not scenario-aware**: it follows a fixed SRE checklist and applies three rules to the actual tool results (temporal deploy correlation with version-tagged errors, CPU saturation, downstream dominance in traces).

## Consequences

- `make demo` and CI always run; results are reproducible.
- The safety claims don't depend on which reasoner runs. The adversarial suite uses a third, hostile, implementation of the same interface.
- Diagnosis quality with a real model is validated separately. One live Claude Sonnet 5.5 run per scenario reached the expected outcome in all three (SYSTEM_DESIGN.md), and the first live attempt found a provider incompatibility (forced tool choice rejected) that the simulated-API tests could not. That is a smoke test, not an evaluation.

## Revisit if

Live-model evaluation becomes part of CI (a nightly job with a key): track the pass rate per scenario and model version there.
