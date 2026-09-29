# ADR-0011: Fleet-wide blast radius: a domain budget, an incident-storm brake and a fleet lock

- **Status:** Accepted. Built in the PoC.
- **Date:** 2026-09-30

## Context

Every earlier guardrail was per target: one action per workload per 30 minutes, a circuit breaker per action and target, and a lease per target. An assessment of the design asked what happens when a shared dependency that the trace graph doesn't show (CoreDNS, the service mesh, a node pool, an availability zone) degrades, and 60 services alert at once. Each resulting incident would pass its own per-target budget, so 60 "individually valid" tier-1 actions could run in minutes. The cluster-capacity check was also read-then-act and not serialized across targets, so two concurrent scale-ups could both pass it.

## Options considered

| Option | For | Against |
|---|---|---|
| Per-target budgets only (status quo) | Simple | Blind to the pattern that matters most during a shared-infrastructure outage |
| A global serial executor (one action at a time, fleet-wide) | Trivially safe | Also serializes verification windows, so one slow recovery blocks all remediation |
| **Per-failure-domain budget and storm brake in policy, plus a short fleet lock around decide-and-apply** | Keeps unrelated domains independent; the thresholds live in Rego next to the other rules; the lock is held for seconds, not through verification | Two more tunables; a busy lock can turn a valid action into report-only |

## Decision

- **Fleet auto-remediation budget:** at most 2 unattended (tier-1) actions per failure domain (cluster/cell) per 10 minutes. After that, every action in the domain needs a human, even ones that would otherwise be tier 1.
- **Incident-storm brake:** if 4 or more incidents open in one failure domain within 10 minutes, automation in that domain pauses (everything needs approval). The ingest pipeline also flags incidents as a suspected shared-infrastructure cause when a batch produces 4 or more in one domain, and the flag is passed to the agent.
- **Fleet lock:** a lease per cluster, taken after the target lease and held only around re-validation and apply (seconds). Fleet counts and the capacity check are re-read under it, so two runs can't both pass them. The lock order is always target, then fleet, so the two can't deadlock. A busy lock is waited on briefly (about 3 s), then the run ends report-only.
- Actions are recorded with their mode (auto or approved) and failure domain; incidents with their domain and open time.

## Consequences

- Tested by the `fleet_storm` attack: three separate incidents in one cell each scale their own root service with valid, relevant evidence. Runs 1 and 2 execute automatically; run 3 hits the fleet budget and goes to a human, who declines. Plus tests for the storm brake, the fleet lock and the shared-cause flag.
- During a genuine shared-infrastructure outage, ASAP stops acting and keeps reporting. That's the intended trade: humans are already paged, and a wrong automated action is most likely exactly then.
- The thresholds (2 actions, 4 incidents, 10 minutes) are guesses for a PoC. In production they'd be tuned per domain from incident history, and the storm brake should also consider signals outside the alert stream (a cloud-provider status page, a mesh control-plane alert).
- Correlation itself still roots incidents at services in the graph. Modeling shared infrastructure as first-class correlation roots, from an infrastructure inventory rather than traces alone, is the next step (design only).

## Revisit if

The brake fires often on unrelated incidents (thresholds too tight for busy domains), or teams want per-service autonomy that the domain budget blocks. Then move to per-domain, per-action-type budgets learned from history.
