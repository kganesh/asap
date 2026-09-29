# ADR-0002: Plan-and-Execute with a bounded ReAct inner loop

- **Status:** Accepted. Built in the PoC; the multi-agent variant is production design.
- **Date:** 2026-09-29

## Context

Root-cause analysis needs both direction (which hypotheses to test) and adaptivity (the next query depends on the last result). Investigations must also be bounded in time and cost, and reviewable by the approver.

## Options considered

| Pattern | Gains | Costs | Failure mode |
|---|---|---|---|
| Pure ReAct | Adapts every step; fewest tokens on easy incidents | No reviewable plan; drifts on long investigations | Anchors on the first plausible signal, typically the most recent deploy |
| **Plan-and-Execute + bounded ReAct** | The plan is auditable, shown to approvers, and bounds the loop | One extra LLM call per run | A stale plan when the first hypothesis fails |
| Multi-agent consensus (two independent diagnoses + a judge) | Catches single-model anchoring; disagreement is itself a signal | ~2-3x tokens and wall-clock; more to audit | Correlated errors when both agents share a prompt and model |

## Decision

Plan-and-Execute outside, ReAct inside. PLAN records ranked hypotheses; INVESTIGATE is a ReAct loop over read tools, capped at 15 steps; a **replan edge** (INVESTIGATE → PLAN, at most 2) fixes the stale-plan failure mode. Multi-agent consensus is reserved for tier-2 proposals on tier-0 services, in production.

## Consequences

- The `db_red_herring` scenario exercises the replan edge: traces show Postgres dominating latency, the agent revises its hypotheses, and it rejects the coincidental deploy.
- The step cap was raised from the plan's 12 to 15 after the red-herring scenario used exactly 12 (8 checklist reads + replan + 2 dependency reads + diagnosis).
- The planning call costs latency and tokens on every incident, even simple ones.

## Revisit if

Evaluation shows plans rarely change the investigation path (the extra call isn't paying for itself), or single-agent misdiagnosis on tier-0 services exceeds the error budget (bring consensus forward).
