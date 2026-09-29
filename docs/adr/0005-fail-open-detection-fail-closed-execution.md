# ADR-0005: Detection fails open, execution fails closed; ASAP sits beside paging

- **Status:** Accepted. Fail-closed execution is built; the paging topology is production design.
- **Date:** 2026-09-29

## Context

ASAP depends on an LLM provider, a policy engine, a state store, telemetry backends and an approval channel. Any of them can fail, and often during the same incident ASAP is responding to. Two properties matter: humans must always be alerted, and nothing may change in production unless every safety dependency is healthy.

## Options considered

| Option | For | Against |
|---|---|---|
| ASAP in the paging path (it triages, then decides whether to page) | Fewer pages; ASAP can enrich before paging | An ASAP outage or bug suppresses pages: a correlated failure during exactly the incidents that matter |
| **ASAP beside paging (Alertmanager fans out to PagerDuty and ASAP independently)** | Paging never depends on ASAP; ASAP annotates the incident | Humans may start work before ASAP's diagnosis arrives; some duplicated effort |
| Fail open on guardrail outage (act if the policy engine is unreachable) | Remediation keeps flowing | Converts a guardrail outage into an open door |

## Decision

Paging is independent of ASAP. Execution fails closed: if the policy engine is missing, erroring or times out, the state store is unavailable, or the approval channel doesn't answer, the run ends as report-only. An LLM outage degrades to report-only (after hedging to a fallback model). It never blocks the page.

## Consequences

- Tested: `policy_engine_down`, `test_failing_closed_without_any_policy_engine`, `test_state_store_outage_fails_closed`, `test_llm_outage_degrades_to_report_only`, `approval_timeout`.
- During a provider outage, ASAP delivers less value exactly when the load is highest. That is accepted; the rules-only report still gives on-call the correlated incident.
- Some incidents are fixed by humans before ASAP finishes; the "still firing?" gates stop ASAP acting on already-resolved incidents.

## Revisit if

Never, for execution. For detection, only if ASAP's own SLOs demonstrate it is more reliable than the direct paging path, which is unlikely to be worth the coupling.
