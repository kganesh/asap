# ADR-0006: Leases, budgets and approvals in a linearizable store; deny under partition

- **Status:** Accepted. Built on SQLite in the PoC; the production store is design.
- **Date:** 2026-09-29

## Context

"At most one action per target per 30 minutes", "one run per incident" and "this approval is for this exact state" are distributed locks and counters. With several workers (and potentially several regions), stale reads cause exactly the failures the guardrails exist to prevent: two runs restarting the same service, or a budget checked on a lagging replica.

## Options considered

| Option | For | Against |
|---|---|---|
| Eventually consistent store (Redis replicas, DynamoDB default reads) | Available during partitions; fast | Two workers can both see "budget available" and both act |
| **Linearizable store (Postgres with row locks, or etcd)** | Correct mutual exclusion and counters | Unavailable to the minority side of a partition; one more critical dependency |
| No shared state (per-worker limits) | Simple | Limits don't hold across workers; restart loops return at fleet scale |

## Decision

Leases, remediation budgets, circuit-breaker state and approvals live in a linearizable store. **Under a partition or store outage, deny** (CP over AP). Dedup and correlation windows, metrics and trace shipping stay eventually consistent, because a brief duplicate there is absorbed by deterministic incident IDs and the leases.

The PoC uses SQLite (single process), with the same interface: `acquire_lease`, `budget`, `circuit_open`, and idempotent execution steps keyed by `(proposal_id, step)`.

## Consequences

- The restart-loop attack is contained across runs: run 1 executes and escalates, and runs 2-3 are denied by the budget and circuit breaker.
- A store outage stops all automated remediation until it recovers. Paging is unaffected (ADR-0005).
- Multi-region: one store and control plane per region acting only on local clusters, so there is no global lock and no automatic cross-region remediation.

## Revisit if

Cross-region automated remediation becomes a requirement; that would need a globally consistent store (Spanner or CockroachDB) or an explicit leader region.
